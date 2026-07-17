"""Purchase orders, RFQs and quotations.
"""
from __future__ import annotations

import datetime

from django.db.models import F, Q, Sum
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.constants import DocumentStatus
from vs_finance.views import resolve_entity
from vs_workflow.models import WorkflowInstance

from .. import purchasing, sourcing
from ..constants import ProcApprovalState
from ..models import (
    PurchaseOrder,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqLine,
    VendorQuotation,
    VendorQuotationLine,
)
from ..serializers import (
    PurchaseOrderSerializer,
    RequestForQuotationSerializer,
    VendorQuotationSerializer,
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
# Purchase orders                                                             #
# --------------------------------------------------------------------------- #

_CLOSED_PO_STATUSES = (DocumentStatus.CANCELLED, DocumentStatus.REVERSED)


def _purchase_order_queryset(entity):
    """Return the PO read shape used by the list and detail endpoints.

    The annotated line totals let status filters operate on receipt progress in SQL;
    related documents are prefetched once so serialising a page never triggers N+1 reads.
    """
    return (
        PurchaseOrder.objects.filter(entity=entity)
        .select_related("vendor", "requisition")
        .annotate(ordered_qty=Sum("lines__quantity"), received_qty=Sum("lines__received_qty"))
        .prefetch_related("lines", "goods_receipts__lines", "vendor_invoices", "source_quotation")
    )


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
    """Build the PO-list KPIs from all entity documents, not the current page."""
    as_of = as_of or datetime.date.today()
    month_start = as_of.replace(day=1)
    prior_month_end = month_start - datetime.timedelta(days=1)
    prior_month_start = prior_month_end.replace(day=1)
    prior_comparable_end = prior_month_start + datetime.timedelta(days=min(as_of.day, prior_month_end.day) - 1)
    open_count = partial_count = awaiting_count = open_value = mtd_value = prior_mtd_value = 0
    rows = _purchase_order_queryset(entity).exclude(status__in=_CLOSED_PO_STATUSES).values(
        "status", "order_date", "total", "ordered_qty", "received_qty",
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
        qs = _filter_purchase_orders(_purchase_order_queryset(entity), request.query_params)
        return self.paginate(request, qs.order_by("-id"), PurchaseOrderSerializer)

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
            currency=_resolve_currency(entity, body.get("currency")),
            actor_user=request.user,
        )
        return success_response(
            "Purchase order created.", data=PurchaseOrderSerializer(po).data, status=201,
        )


class PurchaseOrderDetailView(_ProcBase):
    """docstring-name: Purchase orders"""
    rbac_permission = "procurement.purchase_order.view"

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


class PurchaseOrderSummaryView(_ProcBase):
    """Entity-scoped KPIs for the PO list header (not a paginated list aggregate)."""
    rbac_permission = "procurement.purchase_order.view"

    def get(self, request):
        entity = resolve_entity(request)
        return success_response("Purchase order summary retrieved.", data=purchase_order_summary(entity))


# --------------------------------------------------------------------------- #
# Requests for quotation (sourcing)                                           #
# --------------------------------------------------------------------------- #

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
        qs = RequestForQuotation.objects.filter(entity=entity).prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(rfq_status=status_)
        return self.paginate(request, qs.order_by("-id"), RequestForQuotationSerializer)

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
        rfq = RequestForQuotation.objects.create(
            entity=entity, requisition=requisition,
            title=body.get("title", ""),
            issue_date=_date(body.get("issue_date"), "issue_date", required=True),
            response_due_date=_date(body.get("response_due_date"), "response_due_date"),
            notes=body.get("notes", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
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
                description=ln.get("description", ""),
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                requisition_line=req_line,
                expense_account=_resolve_account(entity, ln.get("expense_account"), "expense_account"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        return success_response(
            "RFQ created.", data=RequestForQuotationSerializer(rfq).data, status=201,
        )


class RfqDetailView(_ProcBase):
    """docstring-name: RFQs"""
    rbac_permission = "procurement.rfq.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        return success_response("RFQ retrieved.", data=RequestForQuotationSerializer(rfq).data)


class RfqIssueView(_ProcBase):
    """docstring-name: Issue an RFQ"""
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.issue_rfq(rfq, actor_user=request.user)
        return success_response("RFQ issued.", data=RequestForQuotationSerializer(rfq).data)


class RfqCancelView(_ProcBase):
    """docstring-name: Cancel an RFQ"""
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.cancel_rfq(rfq, reason=request.data.get("reason", ""), actor_user=request.user)
        return success_response("RFQ cancelled.", data=RequestForQuotationSerializer(rfq).data)


# --------------------------------------------------------------------------- #
# Vendor quotations (sourcing)                                                #
# --------------------------------------------------------------------------- #

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
        qs = VendorQuotation.objects.filter(entity=entity).select_related(
            "vendor", "rfq").prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(quotation_status=status_)
        if (rfq := request.query_params.get("rfq")):
            qs = qs.filter(rfq_id=rfq)
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
        return self.paginate(request, qs.order_by("-id"), VendorQuotationSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=body.get("rfq")).first()
        if rfq is None:
            raise ValidationError({"rfq": "An RFQ is required."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        quotation = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor,
            quote_date=_date(body.get("quote_date"), "quote_date", required=True),
            valid_until=_date(body.get("valid_until"), "valid_until"),
            currency=_resolve_currency(entity, body.get("currency")),
            lead_time_days=body.get("lead_time_days") or None,
            reference=body.get("reference", ""), notes=body.get("notes", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            rfq_line = None
            if ln.get("rfq_line"):
                rfq_line = RfqLine.objects.filter(rfq__entity=entity, pk=ln["rfq_line"]).first()
                if rfq_line is None:
                    raise ValidationError({"rfq_line": f"No such RFQ line {ln['rfq_line']}."})
            expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
                or (rfq_line.expense_account if rfq_line else None)
            VendorQuotationLine.objects.create(
                quotation=quotation, rfq_line=rfq_line, line_no=ln.get("line_no", i),
                description=ln.get("description", rfq_line.description if rfq_line else ""),
                expense_account=expense,
                quantity=_dec(ln.get("quantity", rfq_line.quantity if rfq_line else 1), "quantity"),
                unit_price=_money(ln.get("unit_price", 0), "unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        sourcing.price_quotation(quotation)
        return success_response(
            "Quotation created.", data=VendorQuotationSerializer(quotation).data, status=201,
        )


class QuotationDetailView(_ProcBase):
    """docstring-name: Quotations"""
    rbac_permission = "procurement.quotation.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        return success_response("Quotation retrieved.", data=VendorQuotationSerializer(quotation).data)


class QuotationSubmitView(_ProcBase):
    """docstring-name: Submit a quotation"""
    rbac_permission = "procurement.quotation.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        sourcing.submit_quotation(quotation, actor_user=request.user)
        quotation.refresh_from_db()
        return success_response(
            "Quotation submitted.", data=VendorQuotationSerializer(quotation).data,
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

