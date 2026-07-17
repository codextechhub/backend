"""Purchase requisitions and approval submission.
"""
from __future__ import annotations

import csv
import datetime

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.constants import BudgetStatus, DocumentStatus
from vs_finance.models import Budget, BudgetLine, CostCenter, FiscalPeriod
from vs_finance.views import resolve_entity
from vs_workflow.models import WorkflowInstance

from .. import approvals
from ..constants import ProcApprovalState
from ..models import (
    PurchaseOrder,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    VendorInvoice,
)
from ..serializers import (
    PurchaseOrderSerializer,
    RequisitionSerializer,
    VendorInvoiceSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _dec,
    _money,
    _require_lines,
    _resolve_account,
    _resolve_tax,
)
from .catalog import _resolve_catalog_item

# --------------------------------------------------------------------------- #
# Purchase requisitions                                                       #
# --------------------------------------------------------------------------- #

def _resolve_cost_center(entity, value):
    """Resolve an id/code inside the selected ledger entity."""
    if value in (None, ""):
        return None
    qs = CostCenter.objects.filter(entity=entity, is_active=True)
    cost_center = qs.filter(pk=value).first() if str(value).isdigit() else qs.filter(code=value).first()
    if cost_center is None:
        raise ValidationError({"cost_center": "No such active cost centre in this entity."})
    return cost_center


def _write_requisition_lines(req, entity, lines):
    """Replace a draft's estimate lines from validated entity-scoped references."""
    req.lines.all().delete()
    for i, ln in enumerate(lines, start=1):
        item = _resolve_catalog_item(entity, ln.get("catalog_item"))
        defaults = item.line_defaults() if item else {}
        expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
            or defaults.get("expense_account")
        tax = _resolve_tax(entity, ln.get("tax_code")) or defaults.get("tax_code")
        unit_price = ln.get("estimated_unit_price")
        if unit_price in (None, "") and item is not None:
            unit_price = defaults.get("unit_price", 0)
        PurchaseRequisitionLine.objects.create(
            requisition=req, line_no=ln.get("line_no", i), catalog_item=item,
            description=ln.get("description") or defaults.get("description", ""),
            quantity=_dec(ln.get("quantity", 1), "quantity"),
            unit=str(ln.get("unit") or (item.unit_of_measure if item else "Unit"))[:24],
            estimated_unit_price=_money(unit_price or 0, "estimated_unit_price"),
            expense_account=expense, tax_code=tax,
        )
    # The header estimate is always the exact sum of quantity × unit price on its lines.
    req.recompute_total(save=True)


def _filter_requisitions(qs, params):
    """Apply the list/export filters from one shared source of truth."""
    if (status_ := params.get("status")):
        # A rejected workflow is stored as CANCELLED in the ledger, so the approval overlay disambiguates it.
        qs = qs.filter(approval_state=ProcApprovalState.REJECTED) if status_ == "REJECTED" \
            else qs.filter(status=status_)
    if (search := params.get("search", "").strip()):
        qs = qs.filter(
            Q(document_number__icontains=search) | Q(title__icontains=search)
            | Q(justification__icontains=search) | Q(cost_center__name__icontains=search)
            | Q(requested_by__first_name__icontains=search)
            | Q(requested_by__last_name__icontains=search)
        )
    return qs


def _requisition_display_status(req):
    """Return the user-facing status without losing workflow rejection."""
    # Rejection lives on the approval overlay because the shared document lifecycle has no REJECTED value.
    return "REJECTED" if req.approval_state == ProcApprovalState.REJECTED else req.status

class RequisitionListCreateView(_ProcBase):
    """GET (list) / POST (create draft + lines) purchase requisitions.

    docstring-name: Requisitions
    """

    @property
    def rbac_permission(self):
        return "procurement.requisition.create" if self.request.method == "POST" \
            else "procurement.requisition.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PurchaseRequisition.objects.filter(entity=entity).select_related(
            "requested_by", "cost_center",
        ).prefetch_related("lines")
        qs = _filter_requisitions(qs, request.query_params)
        return self.paginate(request, qs.order_by("-id"), RequisitionSerializer)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        req = PurchaseRequisition.objects.create(
            entity=entity,
            title=str(body.get("title", "")).strip(),
            request_date=_date(body.get("request_date"), "request_date", required=True),
            needed_by=_date(body.get("needed_by"), "needed_by"),
            cost_center=_resolve_cost_center(entity, body.get("cost_center")),
            justification=body.get("justification", ""),
            requested_by=request.user if request.user.is_authenticated else None,
            created_by=request.user if request.user.is_authenticated else None,
        )
        _write_requisition_lines(req, entity, lines)
        return success_response(
            "Requisition created.", data=RequisitionSerializer(req).data, status=201,
        )


class RequisitionDetailView(_ProcBase):
    """docstring-name: Requisitions"""

    @property
    def rbac_permission(self):
        return "procurement.requisition.update" if self.request.method == "PATCH" \
            else "procurement.requisition.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).select_related(
            "requested_by", "cost_center",
        ).prefetch_related("lines").first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        data = RequisitionSerializer(req).data
        # Generic workflow rows link through content type + object id; for_document builds that pair safely.
        instance = WorkflowInstance.objects.for_document(req).order_by("-created_at").first()
        data["workflow_instance_id"] = str(instance.id) if instance else None
        return success_response("Requisition retrieved.", data=data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        if req.status != DocumentStatus.DRAFT:
            raise ValidationError({"status": "Only a draft requisition can be edited."})
        body = request.data
        req.title = str(body.get("title", req.title)).strip()
        req.request_date = _date(body.get("request_date", req.request_date), "request_date", required=True)
        req.needed_by = _date(body.get("needed_by"), "needed_by") if "needed_by" in body else req.needed_by
        req.cost_center = _resolve_cost_center(entity, body.get("cost_center")) \
            if "cost_center" in body else req.cost_center
        req.justification = str(body.get("justification", req.justification)).strip()
        req.save(update_fields=[
            "title", "request_date", "needed_by", "cost_center", "justification", "updated_at",
        ])
        if "lines" in body:
            _write_requisition_lines(req, entity, _require_lines(body))
        return success_response("Requisition updated.", data=RequisitionSerializer(req).data)


class RequisitionSummaryView(_ProcBase):
    """Compact, unpaginated KPI totals for the requisition list header."""
    rbac_permission = "procurement.requisition.view"

    def get(self, request):
        entity = resolve_entity(request)
        as_of = timezone.localdate()
        current_start = as_of.replace(day=1)
        prior_month_end = current_start - datetime.timedelta(days=1)
        prior_start = prior_month_end.replace(day=1)
        # Compare the same number of elapsed calendar days so a partial month is not measured against a full month.
        prior_end = min(prior_month_end, prior_start + datetime.timedelta(days=as_of.day - 1))
        qs = PurchaseRequisition.objects.filter(entity=entity)

        pending = qs.filter(status=DocumentStatus.PENDING_APPROVAL).aggregate(
            count=Count("id"), amount=Sum("estimated_total"),
        )
        approved_mtd = qs.filter(
            status=DocumentStatus.APPROVED, request_date__range=(current_start, as_of),
        ).aggregate(count=Count("id"), amount=Sum("estimated_total"))
        approved_prior = qs.filter(
            status=DocumentStatus.APPROVED, request_date__range=(prior_start, prior_end),
        ).aggregate(count=Count("id"), amount=Sum("estimated_total"))
        drafts = qs.filter(status=DocumentStatus.DRAFT).aggregate(
            count=Count("id"), amount=Sum("estimated_total"),
        )
        active = qs.exclude(status=DocumentStatus.CANCELLED)
        current_total = active.filter(request_date__range=(current_start, as_of)).aggregate(
            amount=Sum("estimated_total"),
        )["amount"] or 0
        prior_total = active.filter(request_date__range=(prior_start, prior_end)).aggregate(
            amount=Sum("estimated_total"),
        )["amount"] or 0

        def percent_change(current, previous):
            # A zero prior period has no valid percentage denominator, so the UI receives null rather than a false 0%.
            return round(((current - previous) / previous) * 100, 1) if previous else None

        return success_response("Requisition summary retrieved.", data={
            "as_of": as_of.isoformat(),
            "pending_approval": {
                "count": pending["count"], "amount": pending["amount"] or 0,
            },
            "approved_mtd": {
                "count": approved_mtd["count"], "amount": approved_mtd["amount"] or 0,
                "change_pct": percent_change(approved_mtd["count"], approved_prior["count"]),
            },
            "draft": {"count": drafts["count"], "amount": drafts["amount"] or 0},
            "total_value_mtd": {
                "amount": current_total, "change_pct": percent_change(current_total, prior_total),
            },
        })


class RequisitionExportView(_ProcBase):
    """Export the full filtered requisition set as a CSV file."""
    rbac_permission = "procurement.requisition.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PurchaseRequisition.objects.filter(entity=entity).select_related(
            "requested_by", "cost_center",
        )
        qs = _filter_requisitions(qs, request.query_params).order_by("-id")
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="purchase-requisitions.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Requisition", "Title", "Department", "Requested by", "Request date",
            "Needed by", "Estimated amount (kobo)", "Status", "Business case",
        ])
        # Iterator keeps memory bounded when an entity exports a large requisition history.
        for req in qs.iterator(chunk_size=500):
            user = req.requested_by
            requested_by = "System" if user is None else (
                getattr(user, "full_name", "") or user.get_full_name() or user.email
            )
            writer.writerow([
                req.document_number, req.title, req.cost_center.name if req.cost_center else "",
                requested_by, req.request_date, req.needed_by or "", req.estimated_total,
                _requisition_display_status(req), req.justification,
            ])
        return response


class RequisitionBudgetAvailabilityView(_ProcBase):
    """Return the approved monthly budget and open-PO commitment for a department."""
    rbac_permission = "procurement.requisition.view"

    def get(self, request):
        entity = resolve_entity(request)
        cost_center = _resolve_cost_center(entity, request.query_params.get("cost_center"))
        if cost_center is None:
            raise ValidationError({"cost_center": "Select a department to check its budget."})
        as_of = _date(request.query_params.get("date"), "date") or timezone.localdate()
        period = FiscalPeriod.objects.filter(
            entity=entity, start_date__lte=as_of, end_date__gte=as_of,
        ).select_related("fiscal_year").first()
        if period is None:
            return success_response("No fiscal period covers this date.", data={
                "has_budget": False, "period": None, "budget": 0, "committed": 0,
                "available": 0,
            })

        # When approved scenarios coexist, the most recently approved plan is the operative budget.
        budget = Budget.objects.filter(
            entity=entity, fiscal_year=period.fiscal_year, status=BudgetStatus.APPROVED,
        ).order_by("-approved_at", "-id").first()
        budget_amount = 0
        if budget is not None:
            budget_amount = BudgetLine.objects.filter(
                budget=budget, cost_center=cost_center, period_no=period.period_no,
            ).aggregate(total=Sum("amount"))["total"] or 0

        # Open PO line values are commitments; the requisition header carries the department into the PO relationship.
        committed = PurchaseOrder.objects.filter(
            entity=entity,
            requisition__cost_center=cost_center,
            status__in=(DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED),
        ).aggregate(total=Sum("lines__net_amount"))["total"] or 0
        # Available budget is the approved plan less purchase commitments already raised for the department.
        available = budget_amount - committed
        return success_response("Budget availability retrieved.", data={
            "has_budget": budget is not None and budget_amount > 0,
            "period": period.name,
            "budget": budget_amount,
            "committed": committed,
            "available": available,
        })


class RequisitionSubmitView(_ProcBase):
    """Submit a requisition into the ``vs_workflow`` approval engine.

    Approval is no longer a direct endpoint — submitting hands the document to its
    threshold-gated workflow template; approvers then vote through the ``vs_workflow``
    API, and the engine's callback drives the requisition to APPROVED.

    docstring-name: Submit a requisition
    """
    rbac_permission = "procurement.requisition.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        instance = approvals.submit_for_approval(req, actor_user=request.user)
        return _approval_response("Requisition submitted for approval.",
                                  req, instance, RequisitionSerializer)


# --------------------------------------------------------------------------- #
# Spend approvals (vs_workflow hand-off)                                       #
# --------------------------------------------------------------------------- #

def _approval_response(message, document, instance, serializer_cls):
    """Build the standard envelope for a submit-for-approval action.

    Re-reads ``document`` because the engine may have reached a terminal decision
    synchronously (all stages auto-skipped), mutating it via a different instance.
    """
    document.refresh_from_db()
    return success_response(message, data={
        "workflow_instance_id": instance.id,
        "workflow_status": instance.status,
        "approval_state": document.approval_state,
        "document": serializer_cls(document).data,
    })


class PurchaseOrderSubmitApprovalView(_ProcBase):
    """docstring-name: Submit a purchase order for approval"""
    rbac_permission = "procurement.purchase_order.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        po = PurchaseOrder.objects.filter(entity=entity, pk=pk).first()
        if po is None:
            raise NotFound("No such purchase order in this entity.")
        instance = approvals.submit_for_approval(po, actor_user=request.user)
        return _approval_response("Purchase order submitted for approval.",
                                  po, instance, PurchaseOrderSerializer)


class VendorInvoiceSubmitApprovalView(_ProcBase):
    """docstring-name: Submit a vendor invoice for approval"""
    rbac_permission = "procurement.vendor_invoice.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        inv = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if inv is None:
            raise NotFound("No such vendor invoice in this entity.")
        instance = approvals.submit_for_approval(inv, actor_user=request.user)
        return _approval_response("Vendor invoice submitted for approval.",
                                  inv, instance, VendorInvoiceSerializer)


class ApprovalTemplateSetupView(_ProcBase):
    """Provision the platform-wide default threshold-gated approval templates.

    POST body (all optional): ``threshold`` (kobo), ``manager_permission``,
    ``senior_permission``. Idempotent — re-running upserts the templates in place.

    docstring-name: Set up approval templates
    """
    rbac_permission = "procurement.approval.manage"

    def post(self, request):
        body = request.data or {}
        kwargs = {}
        if "threshold" in body:
            kwargs["threshold"] = _money(body.get("threshold"), "threshold")
        if body.get("manager_permission"):
            kwargs["manager_permission"] = str(body["manager_permission"])
        if body.get("senior_permission"):
            kwargs["senior_permission"] = str(body["senior_permission"])
        templates = approvals.ensure_default_approval_templates(
            created_by=request.user, **kwargs,
        )
        return success_response("Default approval templates provisioned.", data={
            "templates": [
                {"id": t.id, "document_type": t.document_type, "code": t.code, "name": t.name}
                for t in templates
            ],
        })
