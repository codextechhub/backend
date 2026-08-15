"""Physical receiving, vendor billing, three-way matching, and AP posting.

Draft GRNs and bills are editable evidence only.  Posting a GRN advances receipt
quantities and GR/IR; matching compares the PO, posted receipt, and bill; posting
the approved invoice creates AP.  Keeping those boundaries separate prevents an
approval or edit from silently becoming an accounting mutation.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission

from .. import payables, purchasing
from ..models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    StockItem,
    VendorInvoice,
    VendorInvoiceLine,
)
from ..serializers import (
    GoodsReceivedNoteListSerializer,
    GoodsReceivedNoteSerializer,
    VendorInvoiceListSerializer,
    VendorInvoiceSerializer,
)


from .base import (
    _ProcBase,
    _branch_scoped,
    _date,
    _document_or_404,
    _inherited_branch_id,
    _money,
    _nonneg_qty,
    _quantity,
    _raised_branch,
    _require_lines,
    _resolve_account,
    _resolve_cost_center,
    _resolve_currency,
    _resolve_tax,
    _resolve_vendor,
)

# --------------------------------------------------------------------------- #
# Goods received notes                                                        #
# --------------------------------------------------------------------------- #

def _resolve_stock_item(entity, ref):
    """Resolve an optional active stock master by id or case-insensitive exact code.

    Missing, inactive, and foreign rows deliberately share one field error so the
    tenant-scoped picker cannot be used to discover another entity's stock identifiers.
    """
    if ref in (None, ""):
        return None
    qs = StockItem.objects.filter(entity=entity, is_active=True).select_related(
        "default_expense_account",
    )
    item = (
        qs.filter(pk=int(ref)).first()
        if str(ref).isdigit()
        else qs.filter(code__iexact=str(ref)).first()
    )
    if item is None:
        raise ValidationError({
            "stock_item": "No such active stock item in this entity.",
        })
    return item


def _write_grn_lines(entity, grn, po, lines):
    """Replace a draft receipt's lines from validated, entity-scoped input.

    Shared by create and draft-edit so both enforce the same rules - whole-unit
    counts, per-line PO membership, and the accepted+rejected ≤ PO-remainder cap -
    and so an edit can freely add, drop, or re-key lines (including a direct GRN's
    own lines). A draft never advances the PO's ``received_qty``, so that remainder
    is the same baseline whether the receipt is being created or re-edited, and the
    delete-recreate is safe because nothing references a draft receipt's lines yet.
    """
    grn.lines.all().delete()
    for i, ln in enumerate(lines, start=1):
        accepted = _nonneg_qty(ln.get("accepted_qty", 0), "accepted_qty")
        rejected = _nonneg_qty(ln.get("rejected_qty", 0), "rejected_qty")
        # Physical receipt counts are whole units; reject fractional API input that bypasses the UI steppers.
        if accepted != accepted.to_integral_value() or rejected != rejected.to_integral_value():
            raise ValidationError({"quantity": "Accepted and rejected quantities must be whole numbers."})
        po_line = None
        expected = _nonneg_qty(accepted + rejected, "quantity")
        if ln.get("po_line"):
            po_line = PurchaseOrderLine.objects.filter(
                purchase_order__entity=entity, pk=ln["po_line"],
            ).select_related("cost_center").first()
            if po_line is None:
                raise ValidationError({"po_line": f"No such PO line {ln['po_line']}."})
            if po and po_line.purchase_order_id != po.id:
                raise ValidationError({"po_line": "Each receipt line must belong to the selected purchase order."})
            # Accepted + rejected is the inspected delivery quantity and cannot exceed the PO remainder.
            remaining = Decimal(po_line.quantity) - Decimal(po_line.received_qty)
            if accepted + rejected > remaining:
                raise ValidationError({"quantity": f"Cannot exceed remaining quantity for '{po_line.description}'."})
            # Snapshot the PO remainder so this GRN keeps its own “received of expected” denominator.
            expected = remaining
        explicit_cost_center = _resolve_cost_center(entity, ln.get("cost_center"))
        if po_line and explicit_cost_center and explicit_cost_center.pk != po_line.cost_center_id:
            raise ValidationError({
                "cost_center": "The receipt cost centre must match its purchase-order line.",
            })
        cost_center = po_line.cost_center if po_line else explicit_cost_center
        stock_item = _resolve_stock_item(entity, ln.get("stock_item"))
        expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
            or (po_line.expense_account if po_line else None) \
            or (stock_item.default_expense_account if stock_item else None)
        if expense is None:
            raise ValidationError({"expense_account": "A line expense account is required."})
        unit_price = _money(ln.get("unit_price", po_line.unit_price if po_line else 0), "unit_price")
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=po_line, line_no=ln.get("line_no", i),
            # Keep a display snapshot while falling back to the PO description when older clients omit it.
            description=ln.get("description") or (po_line.description if po_line else ""),
            expense_account=expense,
            stock_item=stock_item,
            cost_center=cost_center,
            accepted_qty=accepted, rejected_qty=rejected, expected_qty=expected,
            unit_price=unit_price,
            # Draft value must be live: accepted whole units × the PO unit price in minor currency units.
            value_amount=int(accepted * unit_price),
        )
    grn.recompute_total(save=True)


def _read_grn_for_response(entity, pk):
    """Re-read a receipt in the serialisation shape (fresh line cache after a rewrite)."""
    return GoodsReceivedNote.objects.filter(entity=entity, pk=pk).select_related(
        "vendor", "purchase_order", "received_by",
    ).prefetch_related(
        "lines__po_line", "lines__cost_center", "lines__stock_item",
        "purchase_order__lines",
    ).first()


class GoodsReceiptListCreateView(_ProcBase):
    """GET (list) / POST (create draft GRN + lines).

    docstring-name: Goods receipts
    """

    @property
    def rbac_permission(self):
        """Require receipt creation for POST and receipt view for GET."""
        return "procurement.goods_receipt.create" if self.request.method == "POST" \
            else "procurement.goods_receipt.view"

    def get(self, request):
        """List entity receipts with the relations needed for progress display."""
        entity = resolve_entity(request)
        qs = GoodsReceivedNote.objects.filter(entity=entity).select_related(
            "vendor", "purchase_order", "received_by", "branch",
        ).prefetch_related("lines__po_line", "lines__cost_center", "purchase_order__lines")
        qs = _branch_scoped(request, entity, qs, request.query_params)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return self.paginate(request, qs.order_by("-id"), GoodsReceivedNoteListSerializer)

    @transaction.atomic
    def post(self, request):
        """Create a draft physical-receipt snapshot without advancing PO quantities."""
        # Invalid line quantities must roll back the receipt header created earlier in this request.
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        from ..settings import resolve_procurement_settings
        policy = resolve_procurement_settings(entity)
        if policy.require_purchase_order_for_receipts and not body.get("purchase_order"):
            raise ValidationError({
                "purchase_order": "This entity requires an approved purchase order for every goods receipt.",
            })
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = None
        if body.get("purchase_order"):
            po = PurchaseOrder.objects.filter(entity=entity, pk=body["purchase_order"]).first()
            if po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
            if po.vendor_id != vendor.id:
                raise ValidationError({"vendor": "The selected vendor must match the purchase order."})
            if po.status != "APPROVED":
                raise ValidationError({"purchase_order": "Goods can only be received against an approved purchase order."})
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            # A receipt against a PO belongs to that PO's branch; a receipt with no
            # PO starts its own chain and captures the branch from the receiver.
            branch_id=(
                _inherited_branch_id(request, po) if po is not None
                else getattr(_raised_branch(request, entity, body), "pk", None)
            ),
            received_date=_date(body.get("received_date"), "received_date", required=True),
            reference=body.get("reference", ""), narration=body.get("narration", ""),
            # Capture the authenticated receiver so the GRN audit is attributable without trusting client input.
            received_by=request.user if request.user.is_authenticated else None,
            created_by=request.user if request.user.is_authenticated else None,
        )
        _write_grn_lines(entity, grn, po, lines)
        return success_response(
            "Goods receipt created.",
            data=GoodsReceivedNoteSerializer(_read_grn_for_response(entity, grn.pk)).data,
            status=201,
        )


class GoodsReceiptDetailView(_ProcBase):
    """Read a GRN or rewrite only its unposted physical-count evidence."""

    @property
    def rbac_permission(self):
        """Separate draft receipt correction from receipt visibility."""
        return "procurement.goods_receipt.update" if self.request.method == "PATCH" \
            else "procurement.goods_receipt.view"

    def get(self, request, pk):
        """Return one entity receipt with its PO and line snapshots."""
        entity = resolve_entity(request)
        grn = _document_or_404(
            request,
            GoodsReceivedNote.objects.filter(entity=entity).select_related(
                "vendor", "purchase_order", "received_by",
            ).prefetch_related(
                "lines__po_line", "lines__cost_center", "lines__stock_item",
                "purchase_order__lines",
            ),
            pk, "No such goods receipt in this entity.",
        )
        return success_response("Goods receipt retrieved.", data=GoodsReceivedNoteSerializer(grn).data)

    @transaction.atomic
    def patch(self, request, pk):
        """Lock and edit a draft; posted receipt history is immutable."""
        entity = resolve_entity(request)
        # Lock only the base row: PostgreSQL rejects FOR UPDATE over the outer joins
        # that select_related on the nullable purchase_order / received_by would add.
        grn = _document_or_404(
            request, GoodsReceivedNote.objects.select_for_update().filter(entity=entity),
            pk, "No such goods receipt in this entity.",
        )
        if grn.status != "DRAFT":
            raise ValidationError({"status": "Only a draft goods receipt can be edited."})
        body = request.data
        if "received_date" in body:
            grn.received_date = _date(body.get("received_date"), "received_date", required=True)
        if "reference" in body:
            grn.reference = str(body.get("reference", ""))
        if "narration" in body:
            grn.narration = str(body.get("narration", ""))
        grn.save(update_fields=["received_date", "reference", "narration", "updated_at"])
        if "lines" in body:
            # Same rewrite path as create - an edit may add, drop, or adjust lines.
            _write_grn_lines(entity, grn, grn.purchase_order, _require_lines(body))
        # Re-read so the response reflects the rewritten lines, not the pre-edit prefetch cache.
        return success_response(
            "Goods receipt draft updated.",
            data=GoodsReceivedNoteSerializer(_read_grn_for_response(entity, grn.pk)).data,
        )


class GoodsReceiptPostView(_ProcBase):
    """POST - post the GRN (Dr expense, Cr GR/IR clearing).

    docstring-name: Post a goods receipt
    """

    rbac_permission = "procurement.goods_receipt.post"

    def post(self, request, pk):
        """Delegate inventory/GRIR effects to the purchasing posting service."""
        entity = resolve_entity(request)
        grn = _document_or_404(
            request, GoodsReceivedNote.objects.filter(entity=entity), pk,
            "No such goods receipt in this entity.",
        )
        purchasing.post_grn(grn, actor_user=request.user)
        grn = _read_grn_for_response(entity, grn.pk)
        return success_response(
            f"Goods receipt {grn.document_number} posted.",
            data=GoodsReceivedNoteSerializer(grn).data,
        )


# --------------------------------------------------------------------------- #
# Vendor invoices (bills)                                                     #
# --------------------------------------------------------------------------- #

def _invoice_queryset(entity):
    """Eager-loaded source for invoice detail (drawer) serialization."""
    return VendorInvoice.objects.filter(entity=entity).select_related(
        "vendor", "purchase_order", "journal", "branch",
    ).prefetch_related(
        "lines__expense_account", "lines__tax_code__paid_account",
        "lines__po_line", "lines__grn_line__grn", "lines__cost_center",
        "allocations__payment", "journal__lines__account",
        "attachments__uploaded_by",
    )


def _invoice_list_queryset(entity):
    """Lighter source for the paginated list - the list serializer drops line/journal
    arrays, so only the vendor + PO needed for the row headline are joined."""
    return VendorInvoice.objects.filter(entity=entity).select_related(
        "vendor", "purchase_order", "branch",
    )


def _invoice_display_filter(qs, value):
    """Map console tabs to persisted lifecycle fields without conflating them."""
    today = timezone.localdate()
    if value == "DRAFT":
        return qs.filter(status="DRAFT", approval_state="NOT_SUBMITTED")
    if value == "PENDING_APPROVAL":
        return qs.filter(approval_state="PENDING")
    if value == "APPROVED":
        return qs.filter(status="DRAFT", approval_state="APPROVED")
    if value == "POSTED":
        return qs.filter(status="POSTED")
    if value == "OVERDUE":
        return qs.filter(status="POSTED", due_date__lt=today).exclude(payment_status="PAID")
    if value == "DISPUTED":
        return qs.filter(match_status__in=("UNDER_RECEIVED", "OVER_BILLED"))
    if value in ("PARTIAL", "PAID"):
        return qs.filter(payment_status=value)
    return qs


def _validate_vendor_reference(entity, vendor, reference, *, exclude_id=None):
    """Reject a duplicate supplier bill number inside the entity/vendor scope."""
    reference = str(reference or "").strip()
    if not reference:
        return reference
    if len(reference) > 64:
        raise ValidationError({"vendor_reference": "Must be 64 characters or fewer."})
    qs = _vendor_reference_matches(entity, reference, exclude_id=exclude_id).filter(vendor=vendor)
    if qs.exists():
        raise ValidationError({"vendor_reference": "This vendor invoice number is already recorded."})
    return reference


def _vendor_reference_matches(entity, reference, *, exclude_id=None):
    """Return exact, case-insensitive reference matches inside one ledger entity."""
    qs = VendorInvoice.objects.filter(
        entity=entity, vendor_reference__iexact=str(reference or "").strip(),
    ).select_related("vendor")
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return qs


def _confirm_cross_vendor_reference(entity, vendor, reference, confirmed, *, exclude_id=None):
    """Require an explicit JSON boolean confirmation for a reference used elsewhere."""
    if not isinstance(confirmed, bool):
        raise ValidationError({
            "confirm_cross_vendor_reference": "Must be true or false.",
        })
    if not reference or confirmed:
        return
    if _vendor_reference_matches(entity, reference, exclude_id=exclude_id).exclude(vendor=vendor).exists():
        raise ValidationError({
            "vendor_reference": (
                "This invoice number is already used by another vendor. "
                "Review the warning and confirm before continuing."
            ),
        })


_VENDOR_REFERENCE_CONSTRAINT = "uniq_proc_vinvoice_entity_vendor_ref_ci"
_VENDOR_INVOICE_IDEMPOTENCY_CONSTRAINT = "uniq_proc_vinvoice_entity_idempotency_key"


def _constraint_name(exc):
    """Read a named constraint across supported database drivers."""
    cause = exc.__cause__
    diag = getattr(cause, "diag", None)
    constraint_name = (
        getattr(diag, "constraint_name", None)
        or getattr(cause, "constraint_name", None)
    )
    if constraint_name:
        return constraint_name
    # MySQL and SQLite expose the key/index name only in the error payload.
    message = str(cause or exc)
    for name in (_VENDOR_REFERENCE_CONSTRAINT, _VENDOR_INVOICE_IDEMPOTENCY_CONSTRAINT):
        if name in message:
            return name
    return ""


def _is_vendor_reference_conflict(exc):
    """Recognize the named invoice-reference constraint across supported drivers."""
    return _constraint_name(exc) == _VENDOR_REFERENCE_CONSTRAINT


def _raise_vendor_reference_conflict(exc):
    """Map only the known reference constraint; preserve unrelated DB failures."""
    if not _is_vendor_reference_conflict(exc):
        raise exc
    raise ValidationError({
        "vendor_reference": "This vendor invoice number is already recorded.",
    }) from exc


def _creation_idempotency(request):
    """Validate and fingerprint the optional standard Idempotency-Key header."""
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    if len(key) > 128:
        raise ValidationError({"idempotency_key": "Must be 128 characters or fewer."})
    if not key:
        return "", ""
    payload = json.dumps(request.data, sort_keys=True, separators=(",", ":"), default=str)
    return key, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _idempotent_invoice(entity, key, request_hash, actor):
    """Return the prior result only when the key, actor, and payload all agree."""
    if not key:
        return None
    invoice = _invoice_queryset(entity).filter(creation_idempotency_key=key).first()
    if invoice is None:
        return None
    actor_id = actor.pk if actor and actor.is_authenticated else None
    if invoice.created_by_id != actor_id or invoice.creation_request_hash != request_hash:
        raise ValidationError({
            "idempotency_key": "This idempotency key was already used for a different request.",
        })
    return invoice


def _write_invoice_lines(entity, invoice, po, lines):
    """Replace draft lines after validating every PO/GRN join inside the entity."""
    invoice.lines.all().delete()
    for i, ln in enumerate(lines, start=1):
        quantity = _quantity(ln.get("quantity", 1), "quantity")
        po_line = grn_line = None
        if ln.get("po_line"):
            po_line = PurchaseOrderLine.objects.filter(
                purchase_order__entity=entity, pk=ln["po_line"],
            ).select_related("purchase_order", "expense_account", "cost_center").first()
            if po_line is None:
                raise ValidationError({"po_line": f"No such PO line {ln['po_line']}."})
            if po is None or po_line.purchase_order_id != po.id:
                raise ValidationError({"po_line": "Every PO-backed invoice line must belong to the selected purchase order."})
        elif po is not None:
            raise ValidationError({"po_line": "Every line on a PO-backed invoice must identify its purchase-order line."})
        if ln.get("grn_line"):
            grn_line = GoodsReceivedNoteLine.objects.filter(
                grn__entity=entity, pk=ln["grn_line"], grn__status="POSTED",
            ).select_related("grn", "po_line", "cost_center").first()
            if grn_line is None:
                raise ValidationError({"grn_line": "The selected goods-receipt line does not exist or is not posted."})
            if grn_line.grn.vendor_id != invoice.vendor_id or grn_line.grn.purchase_order_id != getattr(po, "id", None):
                raise ValidationError({"grn_line": "The goods-receipt line must belong to this vendor and purchase order."})
            if po_line and grn_line.po_line_id != po_line.id:
                raise ValidationError({"grn_line": "The goods-receipt line does not match the selected PO line."})
        explicit_cost_center = _resolve_cost_center(entity, ln.get("cost_center"))
        authoritative_cost_center = (
            po_line.cost_center if po_line and po_line.cost_center_id
            else grn_line.cost_center if grn_line and grn_line.cost_center_id
            else None
        )
        if (
            authoritative_cost_center and explicit_cost_center
            and explicit_cost_center.pk != authoritative_cost_center.pk
        ):
            raise ValidationError({
                "cost_center": "The invoice cost centre must match its purchase-order or receipt line.",
            })
        cost_center = authoritative_cost_center or explicit_cost_center
        expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
            or (po_line.expense_account if po_line else invoice.vendor.default_expense_account)
        if expense is None:
            raise ValidationError({"expense_account": "A line expense account is required."})
        VendorInvoiceLine.objects.create(
            vendor_invoice=invoice, po_line=po_line, grn_line=grn_line,
            line_no=ln.get("line_no", i),
            description=ln.get("description") or (po_line.description if po_line else ""),
            expense_account=expense, quantity=quantity,
            cost_center=cost_center,
            unit_price=_money(ln.get("unit_price", po_line.unit_price if po_line else 0), "unit_price"),
            tax_code=_resolve_tax(entity, ln.get("tax_code")),
        )
    payables.price_vendor_invoice(invoice)
    # Any edit changes the evidence being matched; force a new server-side match.
    invoice.match_status = "NOT_MATCHED"
    invoice.save(update_fields=["match_status", "updated_at"])


def _serialize_invoice_detail(invoice):
    """Enrich the base invoice with safe, authoritative drawer-only data."""
    from vs_finance.models import FinanceAuditLog
    from vs_workflow.models import WorkflowInstance

    data = VendorInvoiceSerializer(invoice).data
    # Workflow, match comparisons, allocations, journal lines, and activity are
    # response overlays; their authoritative state remains in their owning models.
    workflow = WorkflowInstance.all_objects.filter(
        document_type="procurement.vendor_invoice", document_object_id=str(invoice.pk),
    ).order_by("-created_at").first()
    data["workflow_instance_id"] = workflow.id if workflow else None
    comparisons = []
    for line in invoice.lines.all():
        po_line = line.po_line
        grn_line = line.grn_line
        comparisons.append({
            "invoice_line_id": line.id,
            "description": line.description,
            "po_line_id": line.po_line_id,
            "po_quantity": str(po_line.quantity) if po_line else None,
            "received_quantity": str(po_line.received_qty) if po_line else None,
            "previously_invoiced_quantity": str(po_line.invoiced_qty) if po_line else None,
            "invoice_quantity": str(line.quantity),
            "po_unit_price": po_line.unit_price if po_line else None,
            "invoice_unit_price": line.unit_price,
            "grn_number": grn_line.grn.document_number if grn_line else None,
            "grn_accepted_quantity": str(grn_line.accepted_qty) if grn_line else None,
        })
    data["match_comparisons"] = comparisons
    data["payments"] = [{
        "id": allocation.payment_id,
        "document_number": allocation.payment.document_number,
        "payment_date": allocation.payment.payment_date,
        "amount": allocation.amount,
        "status": allocation.payment.status,
    } for allocation in invoice.allocations.all()]
    data["posting_lines"] = [{
        "account_code": line.account.code,
        "account_name": line.account.name,
        "debit": line.debit,
        "credit": line.credit,
    } for line in invoice.journal.lines.all()] if invoice.journal_id else []
    def display_message(log):
        """Render immutable audit metadata without leaking raw JSON or kobo prose."""
        # FinanceAuditLog is immutable; use structured metadata to present legacy
        # minor-unit messages safely without rewriting the authoritative row.
        if log.action == "VENDOR_INVOICE_POSTED" and "total" in (log.metadata or {}):
            from vs_finance.money import format_naira
            return f"Posted bill from {invoice.vendor.code} ({format_naira(log.metadata['total'])})."
        return log.message

    data["activity"] = [{
        "id": log.id, "action": log.action, "message": display_message(log),
        "status": log.status,
        "actor_name": (
            f"{getattr(log.actor, 'first_name', '')} {getattr(log.actor, 'last_name', '')}".strip()
            or getattr(log.actor, "email", "System")
        ) if log.actor_id else "System",
        "created_at": log.created_at,
    } for log in FinanceAuditLog.objects.filter(
        entity=invoice.entity, target_type="VendorInvoice", target_id=str(invoice.pk),
    ).select_related("actor").order_by("-created_at")[:20]]
    return data

class VendorInvoiceReferenceCheckView(_ProcBase):
    """Check an exact supplier reference before a user commits the invoice form."""

    rbac_permission = "procurement.vendor_invoice.view"

    def get(self, request):
        entity = resolve_entity(request)
        vendor = _resolve_vendor(entity, request.query_params.get("vendor"))
        reference = str(request.query_params.get("reference") or "").strip()
        exclude = request.query_params.get("exclude")
        if exclude not in (None, "") and not str(exclude).isdigit():
            raise ValidationError({"exclude": "Must be a vendor invoice id."})
        if not reference:
            return success_response("Vendor invoice number checked.", data={
                "same_vendor_duplicate": None,
                "other_vendor_matches": [],
                "other_vendor_match_count": 0,
            })
        if len(reference) > 64:
            raise ValidationError({"reference": "Must be 64 characters or fewer."})

        matches = _vendor_reference_matches(
            entity, reference, exclude_id=int(exclude) if exclude else None,
        )
        same_vendor = matches.filter(vendor=vendor).first()
        other_vendor_qs = matches.exclude(vendor=vendor).order_by("id")
        other_vendor_match_count = other_vendor_qs.count()

        def match_data(invoice):
            if invoice is None:
                return None
            serialized = VendorInvoiceListSerializer(invoice).data
            return {
                "id": invoice.id,
                "document_number": invoice.document_number,
                "vendor_code": invoice.vendor.code,
                "vendor_name": invoice.vendor.name,
                "invoice_date": invoice.invoice_date,
                "total": invoice.total,
                "status": serialized["display_status"],
            }

        return success_response("Vendor invoice number checked.", data={
            "same_vendor_duplicate": match_data(same_vendor),
            "other_vendor_matches": [match_data(invoice) for invoice in other_vendor_qs[:5]],
            "other_vendor_match_count": other_vendor_match_count,
        })


class VendorInvoiceListCreateView(_ProcBase):
    """GET (list) / POST (create draft bill + lines).

    docstring-name: Vendor invoices
    """

    @property
    def rbac_permission(self):
        """Require bill creation for POST and invoice view for GET."""
        return "procurement.vendor_invoice.create" if self.request.method == "POST" \
            else "procurement.vendor_invoice.view"

    def get(self, request):
        """List entity bills using persisted lifecycle fields for console tabs."""
        entity = resolve_entity(request)
        qs = _branch_scoped(request, entity, _invoice_list_queryset(entity), request.query_params)
        for param in ("status", "payment_status", "match_status"):
            if (val := request.query_params.get(param)):
                qs = qs.filter(**{param: val})
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
        if (display_status := request.query_params.get("display_status")):
            qs = _invoice_display_filter(qs, display_status)
        if (search := request.query_params.get("search", "").strip()):
            qs = qs.filter(Q(document_number__icontains=search) | Q(vendor_reference__icontains=search)
                           | Q(vendor__code__icontains=search) | Q(vendor__name__icontains=search)
                           | Q(purchase_order__document_number__icontains=search))
        return self.paginate(request, qs.order_by("-id"), VendorInvoiceListSerializer)

    @transaction.atomic
    def post(self, request):
        """Create and price a draft bill from entity-validated PO/GRN references."""
        entity = resolve_entity(request)
        body = request.data
        idempotency_key, request_hash = _creation_idempotency(request)
        replay = _idempotent_invoice(
            entity, idempotency_key, request_hash, request.user,
        )
        if replay is not None:
            return success_response(
                "Vendor invoice already created.",
                data=_serialize_invoice_detail(replay),
            )
        lines = _require_lines(body)
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = None
        if body.get("purchase_order"):
            po = PurchaseOrder.objects.filter(entity=entity, pk=body["purchase_order"]).first()
            if po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
            if po.vendor_id != vendor.id:
                raise ValidationError({"vendor": "The selected vendor must match the purchase order."})
        reference = _validate_vendor_reference(entity, vendor, body.get("vendor_reference"))
        _confirm_cross_vendor_reference(
            entity,
            vendor,
            reference,
            body.get("confirm_cross_vendor_reference", False),
        )
        try:
            # Savepoint keeps the surrounding document transaction usable when a
            # concurrent request wins the case-insensitive reference race.
            with transaction.atomic():
                invoice = VendorInvoice.objects.create(
                    entity=entity, vendor=vendor, purchase_order=po,
                    # A bill against a PO belongs to that PO's branch; a bill with
                    # no PO captures the branch from the person entering it.
                    branch_id=(
                        _inherited_branch_id(request, po) if po is not None
                        else getattr(_raised_branch(request, entity, body), "pk", None)
                    ),
                    invoice_date=_date(body.get("invoice_date"), "invoice_date", required=True),
                    due_date=_date(body.get("due_date"), "due_date"),
                    currency=_resolve_currency(entity, body.get("currency")),
                    vendor_reference=reference,
                    creation_idempotency_key=idempotency_key,
                    creation_request_hash=request_hash,
                    narration=body.get("narration", ""),
                    created_by=request.user if request.user.is_authenticated else None,
                )
        except IntegrityError as exc:
            if _constraint_name(exc) == _VENDOR_INVOICE_IDEMPOTENCY_CONSTRAINT:
                replay = _idempotent_invoice(
                    entity, idempotency_key, request_hash, request.user,
                )
                if replay is not None:
                    return success_response(
                        "Vendor invoice already created.",
                        data=_serialize_invoice_detail(replay),
                    )
            _raise_vendor_reference_conflict(exc)
        _write_invoice_lines(entity, invoice, po, lines)
        return success_response(
            "Vendor invoice created.", data=_serialize_invoice_detail(_invoice_queryset(entity).get(pk=invoice.pk)), status=201,
        )


class VendorInvoiceSummaryView(_ProcBase):
    """KPI aggregate for the Vendor Invoices console.

    Counted over the bills the caller's own list returns, so a branch-bound
    payables clerk is never shown another site's overdue balance.
    """
    rbac_permission = "procurement.vendor_invoice.view"

    def get(self, request):
        """Return counts and overdue balance in integer kobo for the visible bills."""
        entity = resolve_entity(request)
        qs = _branch_scoped(
            request, entity, VendorInvoice.objects.filter(entity=entity),
            request.query_params,
        )
        today = timezone.localdate()
        overdue = qs.filter(status="POSTED", due_date__lt=today).exclude(payment_status="PAID")
        data = {
            "as_of": today,
            "under_review": {"count": qs.filter(approval_state="PENDING").count()},
            "approved": {"count": qs.filter(status="DRAFT", approval_state="APPROVED").count()},
            "overdue": {"count": overdue.count(), "amount": overdue.aggregate(v=Sum("total") - Sum("amount_paid"))["v"] or 0},
            "disputed": {"count": qs.filter(match_status__in=("UNDER_RECEIVED", "OVER_BILLED")).count()},
        }
        return success_response("Vendor invoice summary retrieved.", data=data)


class VendorInvoiceDetailView(_ProcBase):
    """Read a bill's workflow/accounting overlay or edit mutable draft evidence."""
    @property
    def rbac_permission(self):
        """Separate draft-bill correction from invoice visibility."""
        return "procurement.vendor_invoice.update" if self.request.method == "PATCH" \
            else "procurement.vendor_invoice.view"

    def get(self, request, pk):
        """Return one entity bill with match, payment, posting, and audit context."""
        entity = resolve_entity(request)
        invoice = _document_or_404(
            request, _invoice_queryset(entity), pk,
            "No such vendor invoice in this entity.",
        )
        return success_response("Vendor invoice retrieved.", data=_serialize_invoice_detail(invoice))

    @transaction.atomic
    def patch(self, request, pk):
        """Replace an unsubmitted/rejected draft and invalidate its prior match."""
        entity = resolve_entity(request)
        # Lock only the invoice row: purchase_order is nullable, and PostgreSQL
        # rejects FOR UPDATE against the nullable side of that outer join.
        invoice = _document_or_404(
            request, VendorInvoice.objects.select_for_update().filter(entity=entity),
            pk, "No such vendor invoice in this entity.",
        )
        if invoice.status != "DRAFT" or invoice.approval_state not in ("NOT_SUBMITTED", "REJECTED"):
            raise ValidationError({"status": "Only an unsubmitted or rejected draft vendor invoice can be edited."})
        body = request.data
        vendor = _resolve_vendor(entity, body.get("vendor", invoice.vendor_id))
        po = invoice.purchase_order
        if "purchase_order" in body:
            po = PurchaseOrder.objects.filter(entity=entity, pk=body.get("purchase_order")).first() if body.get("purchase_order") else None
            if body.get("purchase_order") and po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
        if po and po.vendor_id != vendor.id:
            raise ValidationError({"vendor": "The selected vendor must match the purchase order."})
        invoice.vendor = vendor
        invoice.purchase_order = po
        for field in ("invoice_date", "due_date"):
            if field in body:
                setattr(invoice, field, _date(body.get(field), field, required=field == "invoice_date"))
        if "vendor_reference" in body or vendor.id != invoice.vendor_id:
            invoice.vendor_reference = _validate_vendor_reference(
                entity,
                vendor,
                body.get("vendor_reference", invoice.vendor_reference),
                exclude_id=invoice.id,
            )
            _confirm_cross_vendor_reference(
                entity,
                vendor,
                invoice.vendor_reference,
                body.get("confirm_cross_vendor_reference", False),
                exclude_id=invoice.id,
            )
        if "narration" in body:
            invoice.narration = str(body.get("narration") or "")
        invoice.approval_state = "NOT_SUBMITTED"
        try:
            with transaction.atomic():
                invoice.save(update_fields=[
                    "vendor", "purchase_order", "invoice_date", "due_date",
                    "vendor_reference", "narration", "approval_state", "updated_at",
                ])
        except IntegrityError as exc:
            _raise_vendor_reference_conflict(exc)
        if "lines" in body:
            _write_invoice_lines(entity, invoice, po, _require_lines(body))
        return success_response(
            "Vendor invoice draft updated.",
            data=_serialize_invoice_detail(_invoice_queryset(entity).get(pk=invoice.pk)),
        )


class VendorInvoiceMatchView(_ProcBase):
    """POST - run the three-way match (PO ↔ GRN ↔ bill) and return the status.

    docstring-name: Match a vendor invoice (3-way)
    """

    rbac_permission = "procurement.vendor_invoice.match"

    def post(self, request, pk):
        """Reprice and compare bill evidence without posting the liability."""
        entity = resolve_entity(request)
        invoice = _document_or_404(
            request, _invoice_queryset(entity), pk,
            "No such vendor invoice in this entity.",
        )
        payables.price_vendor_invoice(invoice)
        payables.match_vendor_invoice(invoice, save=True)
        invoice.refresh_from_db()
        return success_response(
            f"Three-way match: {invoice.match_status}.",
            data=_serialize_invoice_detail(_invoice_queryset(entity).get(pk=invoice.pk)),
        )


class VendorInvoicePostView(_ProcBase):
    """POST - post the bill (Dr GR/IR + input VAT, Cr AP). ``allow_variance`` overrides a flag.

    docstring-name: Post a vendor invoice
    """

    rbac_permission = "procurement.vendor_invoice.post"

    @staticmethod
    def _has_variance_override_access(request):
        """Check the dedicated override key in the request's tenant/branch scope."""
        if is_vision_super_admin(request.user):
            return True
        tenant = getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
        return user_has_rbac_permission(
            request.user, "procurement.vendor_invoice.override_variance",
            tenant=tenant or getattr(request.user, "tenant", None),
        )

    def post(self, request, pk):
        """Post an eligible bill; variance override is explicit and permission-gated."""
        entity = resolve_entity(request)
        invoice = _document_or_404(
            request, _invoice_queryset(entity), pk,
            "No such vendor invoice in this entity.",
        )
        allow_variance = request.data.get("allow_variance", False)
        if not isinstance(allow_variance, bool):
            raise ValidationError({"allow_variance": "Expected a JSON boolean."})
        if allow_variance and not self._has_variance_override_access(request):
            raise PermissionDenied("You do not have permission to override invoice match variances.")
        payables.post_vendor_invoice(
            invoice, actor_user=request.user,
            allow_variance=allow_variance,
        )
        invoice.refresh_from_db()
        return success_response(
            f"Vendor invoice {invoice.document_number} posted.",
            data=_serialize_invoice_detail(_invoice_queryset(entity).get(pk=invoice.pk)),
        )
