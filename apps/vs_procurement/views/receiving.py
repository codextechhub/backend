"""Goods receipts and vendor invoices (3-way match).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import payables, purchasing
from ..models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    VendorInvoice,
    VendorInvoiceLine,
)
from ..serializers import (
    GoodsReceivedNoteListSerializer,
    GoodsReceivedNoteSerializer,
    VendorInvoiceSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _dec,
    _money,
    _require_lines,
    _resolve_account,
    _resolve_currency,
    _resolve_tax,
    _resolve_vendor,
)

# --------------------------------------------------------------------------- #
# Goods received notes                                                        #
# --------------------------------------------------------------------------- #

def _write_grn_lines(entity, grn, po, lines):
    """Replace a draft receipt's lines from validated, entity-scoped input.

    Shared by create and draft-edit so both enforce the same rules — whole-unit
    counts, per-line PO membership, and the accepted+rejected ≤ PO-remainder cap —
    and so an edit can freely add, drop, or re-key lines (including a direct GRN's
    own lines). A draft never advances the PO's ``received_qty``, so that remainder
    is the same baseline whether the receipt is being created or re-edited, and the
    delete-recreate is safe because nothing references a draft receipt's lines yet.
    """
    grn.lines.all().delete()
    for i, ln in enumerate(lines, start=1):
        accepted = _dec(ln.get("accepted_qty", 0), "accepted_qty")
        rejected = _dec(ln.get("rejected_qty", 0), "rejected_qty")
        if accepted < 0 or rejected < 0:
            raise ValidationError({"quantity": "Accepted and rejected quantities cannot be negative."})
        # Physical receipt counts are whole units; reject fractional API input that bypasses the UI steppers.
        if accepted != accepted.to_integral_value() or rejected != rejected.to_integral_value():
            raise ValidationError({"quantity": "Accepted and rejected quantities must be whole numbers."})
        po_line = None
        expected = accepted + rejected
        if ln.get("po_line"):
            po_line = PurchaseOrderLine.objects.filter(
                purchase_order__entity=entity, pk=ln["po_line"]).first()
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
        expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
            or (po_line.expense_account if po_line else None)
        if expense is None:
            raise ValidationError({"expense_account": "A line expense account is required."})
        unit_price = _money(ln.get("unit_price", po_line.unit_price if po_line else 0), "unit_price")
        GoodsReceivedNoteLine.objects.create(
            grn=grn, po_line=po_line, line_no=ln.get("line_no", i),
            # Keep a display snapshot while falling back to the PO description when older clients omit it.
            description=ln.get("description") or (po_line.description if po_line else ""),
            expense_account=expense,
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
    ).prefetch_related("lines__po_line", "purchase_order__lines").first()


class GoodsReceiptListCreateView(_ProcBase):
    """GET (list) / POST (create draft GRN + lines).

    docstring-name: Goods receipts
    """

    @property
    def rbac_permission(self):
        return "procurement.goods_receipt.create" if self.request.method == "POST" \
            else "procurement.goods_receipt.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = GoodsReceivedNote.objects.filter(entity=entity).select_related("vendor", "purchase_order", "received_by").prefetch_related("lines__po_line", "purchase_order__lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return self.paginate(request, qs.order_by("-id"), GoodsReceivedNoteListSerializer)

    @transaction.atomic
    def post(self, request):
        # Invalid line quantities must roll back the receipt header created earlier in this request.
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = None
        if body.get("purchase_order"):
            po = PurchaseOrder.objects.filter(entity=entity, pk=body["purchase_order"]).first()
            if po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
            if po.vendor_id != vendor.id:
                raise ValidationError({"vendor": "The selected vendor must match the purchase order."})
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
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
    """docstring-name: Goods receipts"""

    @property
    def rbac_permission(self):
        return "procurement.goods_receipt.update" if self.request.method == "PATCH" \
            else "procurement.goods_receipt.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        grn = GoodsReceivedNote.objects.filter(entity=entity, pk=pk).select_related("vendor", "purchase_order", "received_by").prefetch_related("lines__po_line", "purchase_order__lines").first()
        if grn is None:
            raise NotFound("No such goods receipt in this entity.")
        return success_response("Goods receipt retrieved.", data=GoodsReceivedNoteSerializer(grn).data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        # Lock only the base row: PostgreSQL rejects FOR UPDATE over the outer joins
        # that select_related on the nullable purchase_order / received_by would add.
        grn = GoodsReceivedNote.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if grn is None:
            raise NotFound("No such goods receipt in this entity.")
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
            # Same rewrite path as create — an edit may add, drop, or adjust lines.
            _write_grn_lines(entity, grn, grn.purchase_order, _require_lines(body))
        # Re-read so the response reflects the rewritten lines, not the pre-edit prefetch cache.
        return success_response(
            "Goods receipt draft updated.",
            data=GoodsReceivedNoteSerializer(_read_grn_for_response(entity, grn.pk)).data,
        )


class GoodsReceiptPostView(_ProcBase):
    """POST — post the GRN (Dr expense, Cr GR/IR clearing).

    docstring-name: Post a goods receipt
    """

    rbac_permission = "procurement.goods_receipt.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        grn = GoodsReceivedNote.objects.filter(entity=entity, pk=pk).first()
        if grn is None:
            raise NotFound("No such goods receipt in this entity.")
        purchasing.post_grn(grn, actor_user=request.user)
        grn.refresh_from_db()
        return success_response(
            f"Goods receipt {grn.document_number} posted.",
            data=GoodsReceivedNoteSerializer(grn).data,
        )


# --------------------------------------------------------------------------- #
# Vendor invoices (bills)                                                     #
# --------------------------------------------------------------------------- #

class VendorInvoiceListCreateView(_ProcBase):
    """GET (list) / POST (create draft bill + lines).

    docstring-name: Vendor invoices
    """

    @property
    def rbac_permission(self):
        return "procurement.vendor_invoice.create" if self.request.method == "POST" \
            else "procurement.vendor_invoice.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorInvoice.objects.filter(entity=entity).select_related("vendor").prefetch_related("lines")
        for param in ("status", "payment_status", "match_status"):
            if (val := request.query_params.get(param)):
                qs = qs.filter(**{param: val})
        return self.paginate(request, qs.order_by("-id"), VendorInvoiceSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = None
        if body.get("purchase_order"):
            po = PurchaseOrder.objects.filter(entity=entity, pk=body["purchase_order"]).first()
            if po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
        invoice = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=_date(body.get("invoice_date"), "invoice_date", required=True),
            due_date=_date(body.get("due_date"), "due_date"),
            currency=_resolve_currency(entity, body.get("currency")),
            vendor_reference=body.get("vendor_reference", ""),
            narration=body.get("narration", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            po_line = grn_line = None
            if ln.get("po_line"):
                from ..models import PurchaseOrderLine
                po_line = PurchaseOrderLine.objects.filter(
                    purchase_order__entity=entity, pk=ln["po_line"]).first()
                if po_line is None:
                    raise ValidationError({"po_line": f"No such PO line {ln['po_line']}."})
            if ln.get("grn_line"):
                grn_line = GoodsReceivedNoteLine.objects.filter(
                    grn__entity=entity, pk=ln["grn_line"]).first()
                if grn_line is None:
                    raise ValidationError({"grn_line": f"No such GRN line {ln['grn_line']}."})
            expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
                or (po_line.expense_account if po_line else None)
            if expense is None:
                raise ValidationError({"expense_account": "A line expense account is required."})
            VendorInvoiceLine.objects.create(
                vendor_invoice=invoice, po_line=po_line, grn_line=grn_line,
                line_no=ln.get("line_no", i), description=ln.get("description", ""),
                expense_account=expense,
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                unit_price=_money(ln.get("unit_price", 0), "unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        payables.price_vendor_invoice(invoice)
        return success_response(
            "Vendor invoice created.", data=VendorInvoiceSerializer(invoice).data, status=201,
        )


class VendorInvoiceDetailView(_ProcBase):
    """docstring-name: Vendor invoices"""
    rbac_permission = "procurement.vendor_invoice.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        invoice = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("No such vendor invoice in this entity.")
        return success_response("Vendor invoice retrieved.", data=VendorInvoiceSerializer(invoice).data)


class VendorInvoiceMatchView(_ProcBase):
    """POST — run the three-way match (PO ↔ GRN ↔ bill) and return the status.

    docstring-name: Match a vendor invoice (3-way)
    """

    rbac_permission = "procurement.vendor_invoice.match"

    def post(self, request, pk):
        entity = resolve_entity(request)
        invoice = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("No such vendor invoice in this entity.")
        payables.price_vendor_invoice(invoice)
        payables.match_vendor_invoice(invoice, save=True)
        invoice.refresh_from_db()
        return success_response(
            f"Three-way match: {invoice.match_status}.",
            data=VendorInvoiceSerializer(invoice).data,
        )


class VendorInvoicePostView(_ProcBase):
    """POST — post the bill (Dr GR/IR + input VAT, Cr AP). ``allow_variance`` overrides a flag.

    docstring-name: Post a vendor invoice
    """

    rbac_permission = "procurement.vendor_invoice.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        invoice = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("No such vendor invoice in this entity.")
        payables.post_vendor_invoice(
            invoice, actor_user=request.user,
            allow_variance=bool(request.data.get("allow_variance", False)),
        )
        invoice.refresh_from_db()
        return success_response(
            f"Vendor invoice {invoice.document_number} posted.",
            data=VendorInvoiceSerializer(invoice).data,
        )
