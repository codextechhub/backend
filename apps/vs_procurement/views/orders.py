"""Purchase orders, RFQs and quotations.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.db.models import Count, F, Prefetch, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.constants import DocumentStatus
from vs_finance.views import resolve_entity
from vs_workflow.models import WorkflowInstance

from .. import purchasing, sourcing
from ..constants import ProcApprovalState, QuotationStatus, RfqStatus
from ..models import (
    PurchaseOrder,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqInvitation,
    RfqLine,
    VendorQuotation,
    VendorQuotationLine,
)
from ..serializers import (
    PurchaseOrderListSerializer,
    PurchaseOrderSerializer,
    QuotationDetailSerializer,
    QuotationListSerializer,
    RfqDetailSerializer,
    RfqListSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _dec,
    _lead_time_days,
    _money,
    _quantity,
    _require_lines,
    _resolve_account,
    _resolve_currency,
    _resolve_expense_account,
    _resolve_tax,
    _resolve_vendor,
    _strict_kobo,
    _text,
)

# --------------------------------------------------------------------------- #
# Purchase orders                                                             #
# --------------------------------------------------------------------------- #

_CLOSED_PO_STATUSES = (DocumentStatus.CANCELLED, DocumentStatus.REVERSED)
# Not-yet-issued documents: excluded from the pipeline KPIs (they are not orders a
# vendor is fulfilling), and never eligible for the derived PARTIAL/RECEIVED stages.
_UNISSUED_PO_STATUSES = (DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL)


def _po_base_queryset(entity):
    """Entity-scoped POs with the receipt-progress annotations the filters/KPIs need.

    The annotated line totals let status filters operate on receipt progress in SQL.
    """
    return (
        PurchaseOrder.objects.filter(entity=entity)
        .select_related("vendor", "requisition")
        .annotate(ordered_qty=Sum("lines__quantity"), received_qty=Sum("lines__received_qty"))
    )


def _purchase_order_queryset(entity):
    """Detail read shape — the full document flow the drawer renders, prefetched once."""
    return _po_base_queryset(entity).prefetch_related(
        "lines", "goods_receipts__lines", "vendor_invoices", "source_quotation",
    )


def _purchase_order_list_queryset(entity):
    """List read shape — only what a row and its computed status need. Receipt and
    invoice documents are the drawer's concern, so they are neither prefetched nor
    serialised here (``lines`` stays prefetched because ``display_status`` derives
    from it, but the array itself is dropped by ``PurchaseOrderListSerializer``)."""
    return _po_base_queryset(entity).prefetch_related("lines", "source_quotation")


def _filter_purchase_orders(qs, params):
    """Apply server-side PO filters, including the derived partial-receipt stage."""
    if (status_ := params.get("status")):
        if status_ == "PARTIAL":
            # Quantities are aggregate annotations, so this becomes one grouped SQL query instead of page-local logic.
            qs = qs.exclude(status__in=(DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL)).filter(
                received_qty__gt=0, received_qty__lt=F("ordered_qty"),
            )
        elif status_ == "PENDING_APPROVAL":
            qs = qs.filter(Q(status=DocumentStatus.PENDING_APPROVAL) | Q(approval_state=ProcApprovalState.PENDING))
        elif status_ == "APPROVED":
            # Fully received documents remain approved in the list; only in-progress receipt work moves to Partial.
            qs = qs.filter(status=DocumentStatus.APPROVED).filter(
                Q(received_qty__isnull=True) | Q(received_qty=0) | Q(received_qty__gte=F("ordered_qty")),
            )
        else:
            qs = qs.filter(status=status_)
    if (vendor := params.get("vendor")):
        qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
    if (search := params.get("search", "").strip()):
        qs = qs.filter(
            Q(document_number__icontains=search)
            | Q(vendor__code__icontains=search)
            | Q(vendor__name__icontains=search)
            | Q(requisition__document_number__icontains=search)
        )
    return qs


def purchase_order_summary(entity, *, as_of: datetime.date | None = None) -> dict:
    """Build the PO-list KPIs from all *issued* entity orders, not the current page.

    Drafts and orders still in approval are not commitments a vendor is fulfilling,
    so they are excluded from every count and value here — the same population the
    dashboard's "Open Purchase Orders" KPI reports.
    """
    as_of = as_of or timezone.localdate()
    month_start = as_of.replace(day=1)
    prior_month_end = month_start - datetime.timedelta(days=1)
    prior_month_start = prior_month_end.replace(day=1)
    prior_comparable_end = prior_month_start + datetime.timedelta(days=min(as_of.day, prior_month_end.day) - 1)
    open_count = partial_count = awaiting_count = open_value = mtd_value = prior_mtd_value = 0
    rows = (
        _po_base_queryset(entity)
        .exclude(status__in=_CLOSED_PO_STATUSES + _UNISSUED_PO_STATUSES)
        .exclude(approval_state=ProcApprovalState.PENDING)
        .values("status", "order_date", "total", "ordered_qty", "received_qty")
    )
    for row in rows:
        # Receipt stage remains quantity-based even when a PO has several GRNs against several lines.
        receipt_stage = purchasing.po_receipt_stage(row["ordered_qty"], row["received_qty"])
        total = int(row["total"] or 0)
        if receipt_stage != "RECEIVED":
            open_count += 1
            open_value += total
        if receipt_stage == "PARTIAL":
            partial_count += 1
        if receipt_stage == "AWAITING":
            awaiting_count += 1
        if month_start <= row["order_date"] <= as_of:
            mtd_value += total
        if prior_month_start <= row["order_date"] <= prior_comparable_end:
            prior_mtd_value += total
    # A zero prior period has no meaningful percentage denominator for a trend label.
    change_pct = round((mtd_value - prior_mtd_value) / prior_mtd_value * 100, 1) if prior_mtd_value else None
    return {
        "as_of": as_of.isoformat(),
        "open": {"count": open_count, "amount": open_value},
        "partially_received": {"count": partial_count},
        "awaiting_receipt": {"count": awaiting_count},
        "po_value_mtd": {"amount": mtd_value, "change_pct": change_pct},
    }

class PurchaseOrderListCreateView(_ProcBase):
    """GET (list) / POST (create from an approved requisition).

    docstring-name: Purchase orders
    """

    @property
    def rbac_permission(self):
        return "procurement.purchase_order.create" if self.request.method == "POST" \
            else "procurement.purchase_order.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _filter_purchase_orders(_purchase_order_list_queryset(entity), request.query_params)
        return self.paginate(request, qs.order_by("-id"), PurchaseOrderListSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        req = PurchaseRequisition.objects.filter(entity=entity, pk=body.get("requisition")).first()
        if req is None:
            raise ValidationError({"requisition": "An approved requisition is required."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = purchasing.create_po_from_requisition(
            req, vendor=vendor,
            order_date=_date(body.get("order_date"), "order_date", required=True),
            expected_date=_date(body.get("expected_date"), "expected_date"),
            delivery_address=str(body.get("delivery_address", "")).strip(),
            payment_terms=str(body.get("payment_terms", vendor.payment_terms)).strip(),
            currency=_resolve_currency(entity, body.get("currency")),
            actor_user=request.user,
        )
        return success_response(
            "Purchase order created.", data=PurchaseOrderSerializer(po).data, status=201,
        )


class PurchaseOrderDetailView(_ProcBase):
    """docstring-name: Purchase orders"""

    @property
    def rbac_permission(self):
        return "procurement.purchase_order.update" if self.request.method == "PATCH" \
            else "procurement.purchase_order.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        po = _purchase_order_queryset(entity).filter(pk=pk).first()
        if po is None:
            raise NotFound("No such purchase order in this entity.")
        data = PurchaseOrderSerializer(po).data
        # Generic workflow rows use content type + object id, which for_document resolves safely.
        instance = WorkflowInstance.objects.for_document(po).order_by("-created_at").first()
        data["workflow_instance_id"] = str(instance.id) if instance else None
        return success_response("Purchase order retrieved.", data=data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        # Lock only the base row because PostgreSQL rejects row locks over grouped or nullable-join detail queries.
        po = PurchaseOrder.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if po is None:
            raise NotFound("No such purchase order in this entity.")
        # The workflow overlay can be PENDING while the finance document status still reads DRAFT.
        if po.status != DocumentStatus.DRAFT or po.approval_state == ProcApprovalState.PENDING:
            raise ValidationError({"status": "Only a draft purchase order can be edited."})

        body = request.data
        if "vendor" in body:
            candidate = _resolve_vendor(entity, body.get("vendor"))
            if reason := purchasing.vendor_purchase_block_reason(candidate):
                raise ValidationError({"vendor": reason})
            po.vendor = candidate
        if "order_date" in body:
            po.order_date = _date(body.get("order_date"), "order_date", required=True)
        if "expected_date" in body:
            po.expected_date = _date(body.get("expected_date"), "expected_date")
        if "delivery_address" in body:
            po.delivery_address = str(body.get("delivery_address", "")).strip()
        if "payment_terms" in body:
            po.payment_terms = str(body.get("payment_terms", "")).strip()
        # PO lines remain the approved requisition snapshot; draft edits only change order terms.
        po.save(update_fields=[
            "vendor", "order_date", "expected_date", "delivery_address",
            "payment_terms", "updated_at",
        ])
        # Re-read with the drawer's related-document shape after the locked update has completed.
        updated = _purchase_order_queryset(entity).filter(pk=po.pk).first()
        return success_response(
            "Purchase order draft updated.", data=PurchaseOrderSerializer(updated).data,
        )


class PurchaseOrderSummaryView(_ProcBase):
    """Entity-scoped KPIs for the PO list header (not a paginated list aggregate)."""
    rbac_permission = "procurement.purchase_order.view"

    def get(self, request):
        entity = resolve_entity(request)
        return success_response("Purchase order summary retrieved.", data=purchase_order_summary(entity))


# --------------------------------------------------------------------------- #
# Requests for quotation (sourcing)                                           #
# --------------------------------------------------------------------------- #

def _rfq_list_queryset(entity):
    """Entity-scoped RFQ list with the counts the list row needs, as annotations.

    ``line_count`` and ``response_count`` are computed once in SQL (not per-row) so a
    long RFQ list stays a single query. A *response* is any non-draft quotation — a
    vendor's actual reply, not a half-captured draft.
    """
    return RequestForQuotation.objects.filter(entity=entity).select_related("requisition").annotate(
        line_count=Count("lines", distinct=True),
        response_count=Count(
            "quotations",
            filter=~Q(quotations__quotation_status=QuotationStatus.DRAFT),
            distinct=True,
        ),
        invited_count=Count("invitations", distinct=True),
    )


def _rfq_detail_queryset(entity):
    """Entity-scoped RFQ prefetched for the detail drawer (lines + invitations + quotes)."""
    return RequestForQuotation.objects.filter(entity=entity).select_related("requisition").prefetch_related(
        "lines", "lines__expense_account",
        # Invited vendors + quotations are joined in Python in the serializer to derive
        # each invitation's "responded" flag without a per-row query.
        "invitations__vendor",
        # Quotations are re-sorted by total in the serializer (in Python) to reuse this cache.
        Prefetch("quotations", queryset=VendorQuotation.objects.select_related("vendor")),
    )


def _write_rfq_lines(entity, rfq, lines):
    """Validate and (re)create an RFQ's spec lines — a full replacement on edit.

    Shared by create and the draft PATCH so both apply identical validation:
    positive/bounded quantity, active-postable EXPENSE account, entity-scoped tax code,
    and a requisition line that genuinely lives in this entity.
    """
    rfq.lines.all().delete()  # Full replace: the payload is the new authoritative line set.
    for i, ln in enumerate(lines, start=1):
        req_line = None
        if ln.get("requisition_line"):
            req_line = PurchaseRequisitionLine.objects.filter(
                requisition__entity=entity, pk=ln["requisition_line"]).first()
            if req_line is None:
                raise ValidationError(
                    {"requisition_line": f"No such requisition line {ln['requisition_line']}."})
        RfqLine.objects.create(
            rfq=rfq, line_no=ln.get("line_no", i),
            description=_text(ln.get("description"), "description", 255, required=True),
            quantity=_quantity(ln.get("quantity", 1), "quantity"),
            requisition_line=req_line,
            expense_account=_resolve_expense_account(entity, ln.get("expense_account"), "expense_account"),
            tax_code=_resolve_tax(entity, ln.get("tax_code")),
        )


def _validate_rfq_dates(issue_date, response_due_date):
    # A closing date must not precede the issue date.
    if response_due_date is not None and response_due_date < issue_date:
        raise ValidationError({"response_due_date": "Response due date cannot be before the issue date."})


def _resolve_invited_vendors(entity, raw):
    """Resolve an ``invited_vendors`` payload (list of codes or ids) to Vendor objects.

    Each reference is resolved inside ``entity`` (unknown/cross-entity → 400 via
    ``_resolve_vendor``); eligibility + de-duplication are enforced downstream by
    :func:`vs_procurement.sourcing.set_rfq_invitations`.
    """
    if not isinstance(raw, list):
        raise ValidationError({"invited_vendors": "Expected a list of vendor codes or ids."})
    return [_resolve_vendor(entity, ref) for ref in raw]


def _budget_estimate(value, field="budget_estimate"):
    """Optional integer-kobo budget ceiling; strictly rejects float/negative amounts."""
    if value in (None, ""):
        return None
    return _strict_kobo(value, field)


class RfqListCreateView(_ProcBase):
    """GET (list) / POST (create draft RFQ + lines).

    docstring-name: RFQs
    """

    @property
    def rbac_permission(self):
        return "procurement.rfq.create" if self.request.method == "POST" \
            else "procurement.rfq.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _rfq_list_queryset(entity)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(rfq_status=status_)
        if (q := (request.query_params.get("q") or request.query_params.get("search") or "").strip()):
            qs = qs.filter(Q(document_number__icontains=q) | Q(title__icontains=q))
        return self.paginate(request, qs.order_by("-id"), RfqListSerializer)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        requisition = None
        if body.get("requisition"):
            requisition = PurchaseRequisition.objects.filter(
                entity=entity, pk=body["requisition"]).first()
            if requisition is None:
                raise ValidationError({"requisition": "No such requisition in this entity."})
        issue_date = _date(body.get("issue_date"), "issue_date", required=True)
        response_due_date = _date(body.get("response_due_date"), "response_due_date")
        _validate_rfq_dates(issue_date, response_due_date)
        rfq = RequestForQuotation.objects.create(
            entity=entity, requisition=requisition,
            title=_text(body.get("title"), "title", 200),
            issue_date=issue_date, response_due_date=response_due_date,
            budget_estimate=_budget_estimate(body.get("budget_estimate")),
            notes=_text(body.get("notes"), "notes", 255),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _write_rfq_lines(entity, rfq, lines)
        # Invited vendors may be empty at draft-create (issue is what requires ≥1); still
        # validate + persist any provided so the draft carries its addressee list.
        if "invited_vendors" in body:
            sourcing.set_rfq_invitations(
                rfq, _resolve_invited_vendors(entity, body["invited_vendors"]),
                actor_user=request.user,
            )
        rfq = _rfq_detail_queryset(entity).get(pk=rfq.pk)
        return success_response(
            "RFQ created.", data=RfqDetailSerializer(rfq).data, status=201,
        )


class RfqDetailView(_ProcBase):
    """GET (detail) / PATCH (edit a DRAFT RFQ's header + full line replacement).

    docstring-name: RFQs
    """

    @property
    def rbac_permission(self):
        return "procurement.rfq.update" if self.request.method == "PATCH" \
            else "procurement.rfq.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        rfq = _rfq_detail_queryset(entity).filter(pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        return success_response("RFQ retrieved.", data=RfqDetailSerializer(rfq).data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        # Only a draft is editable; once issued its lines are a firm invitation vendors quote against.
        if rfq.rfq_status != RfqStatus.DRAFT:
            raise ValidationError(
                {"rfq_status": f"Only a draft RFQ can be edited (this one is '{rfq.rfq_status}')."})
        body = request.data
        if "title" in body:
            rfq.title = _text(body.get("title"), "title", 200)
        if "issue_date" in body:
            rfq.issue_date = _date(body.get("issue_date"), "issue_date", required=True)
        if "response_due_date" in body:
            rfq.response_due_date = _date(body.get("response_due_date"), "response_due_date")
        if "notes" in body:
            rfq.notes = _text(body.get("notes"), "notes", 255)
        if "budget_estimate" in body:
            rfq.budget_estimate = _budget_estimate(body.get("budget_estimate"))
        _validate_rfq_dates(rfq.issue_date, rfq.response_due_date)
        rfq.save(update_fields=[
            "title", "issue_date", "response_due_date", "budget_estimate", "notes", "updated_at",
        ])
        if "lines" in body:
            _write_rfq_lines(entity, rfq, _require_lines(body))
        # Replacing the invite set is subject to the responded-vendor protection in the service.
        if "invited_vendors" in body:
            sourcing.set_rfq_invitations(
                rfq, _resolve_invited_vendors(entity, body["invited_vendors"]),
                actor_user=request.user,
            )
        rfq = _rfq_detail_queryset(entity).get(pk=rfq.pk)
        return success_response("RFQ updated.", data=RfqDetailSerializer(rfq).data)


class RfqIssueView(_ProcBase):
    """docstring-name: Issue an RFQ"""
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.issue_rfq(rfq, actor_user=request.user)
        rfq = _rfq_detail_queryset(entity).get(pk=rfq.pk)
        return success_response("RFQ issued.", data=RfqDetailSerializer(rfq).data)


class RfqCloseView(_ProcBase):
    """POST — close an ISSUED RFQ without an award; rejects its live quotations.

    docstring-name: Close an RFQ
    """
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.close_rfq(rfq, reason=request.data.get("reason", ""), actor_user=request.user)
        rfq = _rfq_detail_queryset(entity).get(pk=rfq.pk)
        return success_response("RFQ closed.", data=RfqDetailSerializer(rfq).data)


class RfqCancelView(_ProcBase):
    """docstring-name: Cancel an RFQ"""
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.cancel_rfq(rfq, reason=request.data.get("reason", ""), actor_user=request.user)
        rfq = _rfq_detail_queryset(entity).get(pk=rfq.pk)
        return success_response("RFQ cancelled.", data=RfqDetailSerializer(rfq).data)


class RfqSummaryView(_ProcBase):
    """Entity-scoped KPI counts for the RFQ list header (Draft · Open · Responses · Closing)."""
    rbac_permission = "procurement.rfq.view"

    def get(self, request):
        entity = resolve_entity(request)
        today = timezone.localdate()
        # One aggregate over the RFQ table for the three RFQ-status counts.
        counts = RequestForQuotation.objects.filter(entity=entity).aggregate(
            draft=Count("id", filter=Q(rfq_status=RfqStatus.DRAFT)),
            open=Count("id", filter=Q(rfq_status=RfqStatus.ISSUED)),
            closing_soon=Count("id", filter=Q(
                rfq_status=RfqStatus.ISSUED,
                response_due_date__gte=today,
                response_due_date__lte=today + datetime.timedelta(days=7),
            )),
        )
        # A second cheap query: submitted responses currently sitting on issued RFQs.
        responses_in = VendorQuotation.objects.filter(
            entity=entity, rfq__rfq_status=RfqStatus.ISSUED,
        ).exclude(quotation_status=QuotationStatus.DRAFT).count()
        return success_response("RFQ summary retrieved.", data={
            "draft": counts["draft"] or 0,
            "open": counts["open"] or 0,
            "responses_in": responses_in,
            "closing_soon": counts["closing_soon"] or 0,
        })


# --------------------------------------------------------------------------- #
# Vendor quotations (sourcing)                                                #
# --------------------------------------------------------------------------- #

def _quotation_detail_queryset(entity):
    return VendorQuotation.objects.filter(entity=entity).select_related(
        "vendor", "rfq", "awarded_po",
    ).prefetch_related("lines", "lines__expense_account")


def _write_quotation_lines(entity, quotation, rfq, lines):
    """Validate and (re)create a quotation's priced lines — full replacement on edit.

    Every ``rfq_line`` reference must belong to *this* RFQ (not merely the entity),
    closing a cross-RFQ line-leak: a quote may only price lines of the RFQ it answers.
    """
    quotation.lines.all().delete()
    for i, ln in enumerate(lines, start=1):
        rfq_line = None
        if ln.get("rfq_line"):
            # Scope to the referenced RFQ, so a line from a different RFQ cannot be smuggled in.
            rfq_line = RfqLine.objects.filter(rfq=rfq, pk=ln["rfq_line"]).first()
            if rfq_line is None:
                raise ValidationError({"rfq_line": f"No such RFQ line {ln['rfq_line']} on this RFQ."})
        expense = _resolve_expense_account(entity, ln.get("expense_account"), "expense_account") \
            or (rfq_line.expense_account if rfq_line else None)
        VendorQuotationLine.objects.create(
            quotation=quotation, rfq_line=rfq_line, line_no=ln.get("line_no", i),
            description=_text(
                ln.get("description", rfq_line.description if rfq_line else ""),
                "description", 255, required=True,
            ),
            expense_account=expense,
            quantity=_quantity(ln.get("quantity", rfq_line.quantity if rfq_line else 1), "quantity"),
            unit_price=_strict_kobo(ln.get("unit_price", 0), "unit_price"),
            tax_code=_resolve_tax(entity, ln.get("tax_code")),
        )


def _validate_quote_dates(quote_date, valid_until):
    if valid_until is not None and valid_until < quote_date:
        raise ValidationError({"valid_until": "Valid-until date cannot be before the quote date."})


class QuotationListCreateView(_ProcBase):
    """GET (list) / POST (create draft quotation + priced lines) against an RFQ.

    docstring-name: Quotations
    """

    @property
    def rbac_permission(self):
        return "procurement.quotation.create" if self.request.method == "POST" \
            else "procurement.quotation.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorQuotation.objects.filter(entity=entity).select_related("vendor", "rfq")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(quotation_status=status_)
        if (rfq := request.query_params.get("rfq")):
            qs = qs.filter(rfq_id=rfq)
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
        if (q := (request.query_params.get("q") or request.query_params.get("search") or "").strip()):
            qs = qs.filter(
                Q(document_number__icontains=q) | Q(reference__icontains=q)
                | Q(vendor__name__icontains=q) | Q(vendor__code__icontains=q)
                | Q(rfq__document_number__icontains=q)
            )
        return self.paginate(request, qs.order_by("-id"), QuotationListSerializer)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=body.get("rfq")).first()
        if rfq is None:
            raise ValidationError({"rfq": "An RFQ is required."})
        # A quotation is an offer against a *live* invitation — the RFQ must be issued.
        if rfq.rfq_status != RfqStatus.ISSUED:
            raise ValidationError(
                {"rfq": f"Quotations can only be captured against an ISSUED RFQ (this one is '{rfq.rfq_status}')."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        # Governance gate: an inactive / on-hold / KYC-rejected vendor cannot enter contention.
        if reason := purchasing.vendor_purchase_block_reason(vendor):
            raise ValidationError({"vendor": reason})
        # Invited-only: an RFQ is a request sent to invited vendors, so only an invited
        # vendor may quote against it.
        if not RfqInvitation.objects.filter(rfq=rfq, vendor=vendor).exists():
            raise ValidationError(
                {"vendor": f"Vendor {vendor.code} is not invited to RFQ {rfq.document_number}."})
        quote_date = _date(body.get("quote_date"), "quote_date", required=True)
        valid_until = _date(body.get("valid_until"), "valid_until")
        _validate_quote_dates(quote_date, valid_until)
        quotation = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor,
            quote_date=quote_date, valid_until=valid_until,
            currency=_resolve_currency(entity, body.get("currency")),
            lead_time_days=_lead_time_days(body.get("lead_time_days")),
            reference=_text(body.get("reference"), "reference", 64),
            notes=_text(body.get("notes"), "notes", 255),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _write_quotation_lines(entity, quotation, rfq, lines)
        sourcing.price_quotation(quotation)
        quotation = _quotation_detail_queryset(entity).get(pk=quotation.pk)
        return success_response(
            "Quotation created.", data=QuotationDetailSerializer(quotation).data, status=201,
        )


class QuotationDetailView(_ProcBase):
    """GET (detail) / PATCH (edit a DRAFT quotation's header + priced-line replacement).

    docstring-name: Quotations
    """

    @property
    def rbac_permission(self):
        return "procurement.quotation.update" if self.request.method == "PATCH" \
            else "procurement.quotation.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        quotation = _quotation_detail_queryset(entity).filter(pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        return success_response("Quotation retrieved.", data=QuotationDetailSerializer(quotation).data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.select_for_update().select_related("rfq").filter(
            entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        # Submitted/awarded/rejected offers are firm and immutable — only a draft edits.
        if quotation.quotation_status != QuotationStatus.DRAFT:
            raise ValidationError(
                {"quotation_status": f"Only a draft quotation can be edited (this one is "
                                     f"'{quotation.quotation_status}')."})
        body = request.data
        if "quote_date" in body:
            quotation.quote_date = _date(body.get("quote_date"), "quote_date", required=True)
        if "valid_until" in body:
            quotation.valid_until = _date(body.get("valid_until"), "valid_until")
        if "lead_time_days" in body:
            quotation.lead_time_days = _lead_time_days(body.get("lead_time_days"))
        if "reference" in body:
            quotation.reference = _text(body.get("reference"), "reference", 64)
        if "notes" in body:
            quotation.notes = _text(body.get("notes"), "notes", 255)
        _validate_quote_dates(quotation.quote_date, quotation.valid_until)
        quotation.save(update_fields=[
            "quote_date", "valid_until", "lead_time_days", "reference", "notes", "updated_at",
        ])
        if "lines" in body:
            _write_quotation_lines(entity, quotation, quotation.rfq, _require_lines(body))
        sourcing.price_quotation(quotation)  # Re-roll net/tax/totals after any edit.
        quotation = _quotation_detail_queryset(entity).get(pk=quotation.pk)
        return success_response("Quotation updated.", data=QuotationDetailSerializer(quotation).data)


class QuotationSubmitView(_ProcBase):
    """docstring-name: Submit a quotation"""
    rbac_permission = "procurement.quotation.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        sourcing.submit_quotation(quotation, actor_user=request.user)
        quotation = _quotation_detail_queryset(entity).get(pk=quotation.pk)
        return success_response(
            "Quotation submitted.", data=QuotationDetailSerializer(quotation).data,
        )


class QuotationAwardView(_ProcBase):
    """POST — award the quotation: build a DRAFT PO and reject the losing quotes.

    docstring-name: Award a quotation
    """

    rbac_permission = "procurement.quotation.award"

    def post(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        po = sourcing.award_quotation(
            quotation,
            order_date=_date(request.data.get("order_date"), "order_date"),
            actor_user=request.user,
        )
        return success_response(
            f"Quotation awarded → purchase order {po.document_number}.",
            data=PurchaseOrderSerializer(po).data, status=201,
        )
