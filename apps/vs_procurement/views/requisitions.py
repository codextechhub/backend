"""Purchase intent, budget availability, and workflow submission boundaries.

Requisitions are editable estimates until submitted.  Approval authorizes the
intent but does not itself create a PO, receive goods, or post accounting.  Every
line default is snapshotted from entity-scoped master data and the header total is
derived server-side in integer kobo.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.response import success_response
from vs_finance.constants import BudgetStatus, DocumentStatus
from vs_finance.models import Budget, BudgetLine, FiscalPeriod
from vs_finance.views import resolve_entity
from vs_workflow.models import WorkflowInstance
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission

from .. import approval_override, approval_parking, approvals
from ..constants import ProcApprovalState
from ..models import (
    PurchaseOrder,
    PurchaseOrderLine,
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
    _branch_scoped,
    _date,
    _document_or_404,
    _money,
    _quantity,
    _raised_branch,
    _require_lines,
    _resolve_account,
    _resolve_cost_center,
    _resolve_tax,
)
from .catalog import _resolve_catalog_item

# --------------------------------------------------------------------------- #
# Purchase requisitions                                                       #
# --------------------------------------------------------------------------- #

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
            quantity=_quantity(ln.get("quantity", 1), "quantity"),
            unit=str(ln.get("unit") or (item.unit_of_measure if item else "Unit"))[:24],
            estimated_unit_price=_money(unit_price or 0, "estimated_unit_price"),
            expense_account=expense, tax_code=tax,
        )
    # The header estimate is always the exact sum of quantity × unit price on its lines.
    req.recompute_total(save=True)


def _bool_param(value):
    """Parse a permissive truthy/falsey query-string flag, or None when absent/blank."""
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    return None


def _filter_requisitions(qs, params):
    """Apply the list/export filters from one shared source of truth."""
    if (status_ := params.get("status")):
        # A rejected workflow is stored as CANCELLED in the ledger, so the approval overlay disambiguates it.
        qs = qs.filter(approval_state=ProcApprovalState.REJECTED) if status_ == "REJECTED" \
            else qs.filter(status=status_)
    parked = _bool_param(params.get("parked"))
    if parked is not None:
        # Parked = submitted but its active approval stage has an empty approver
        # snapshot, so no one can decide it. Expressed as a subquery so the filter
        # stays bounded however many documents a misconfigured tenant has parked.
        parked_pks = approval_parking.parked_document_id_subquery(PurchaseRequisition)
        qs = qs.filter(pk__in=parked_pks) if parked else qs.exclude(pk__in=parked_pks)
    overridden = _bool_param(params.get("approved_by_override"))
    if overridden is not None:
        # Approved without review: a parked stage released by a permissioned human
        # instead of decided. Same bounded-subquery shape as the parked filter.
        override_pks = approval_override.overridden_document_id_subquery(PurchaseRequisition)
        qs = qs.filter(pk__in=override_pks) if overridden else qs.exclude(pk__in=override_pks)
    if (search := params.get("search", "").strip()):
        qs = qs.filter(
            Q(document_number__icontains=search) | Q(title__icontains=search)
            | Q(justification__icontains=search) | Q(cost_center__name__icontains=search)
            | Q(cost_center__code__icontains=search)
            | Q(requested_by__first_name__icontains=search)
            | Q(requested_by__last_name__icontains=search)
            | Q(requested_by__email__icontains=search)
            | Q(lines__description__icontains=search)
            | Q(lines__catalog_item__name__icontains=search)
            | Q(lines__catalog_item__code__icontains=search)
        )
        # Line-item joins can match several rows on one requisition; distinct keeps the paginated list unique.
        qs = qs.distinct()
    return qs


class RequisitionListCreateView(_ProcBase):
    """GET (list) / POST (create draft + lines) purchase requisitions.

    docstring-name: Requisitions
    """

    @property
    def rbac_permission(self):
        """Require create permission for POST and requisition view for GET."""
        return "procurement.requisition.create" if self.request.method == "POST" \
            else "procurement.requisition.view"

    def get(self, request):
        """List entity requisitions with requester, cost center, and lines preloaded."""
        entity = resolve_entity(request)
        qs = PurchaseRequisition.objects.filter(entity=entity).select_related(
            "requested_by", "cost_center", "branch",
        ).prefetch_related("lines")
        qs = _branch_scoped(request, entity, qs, request.query_params)
        qs = _filter_requisitions(qs, request.query_params)
        return self.paginate(
            request, qs.order_by("-id"), RequisitionSerializer,
            # One pass per page: repair anything repairable, then report what is
            # still parked. Rows that are not PENDING approval cost nothing.
            page_context=lambda page: {
                "parked_requisition_ids": approval_parking.parked_document_ids(page),
                "overridden_requisition_ids": approval_override.overridden_document_ids(page),
            },
        )

    @transaction.atomic
    def post(self, request):
        """Create a draft estimate and its server-priced line snapshot atomically."""
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        from ..settings import resolve_procurement_settings
        policy = resolve_procurement_settings(entity)
        request_date = _date(body.get("request_date"), "request_date", required=True)
        needed_by = _date(body.get("needed_by"), "needed_by")
        if needed_by is None and policy.default_requisition_lead_days:
            needed_by = request_date + datetime.timedelta(
                days=policy.default_requisition_lead_days,
            )
        req = PurchaseRequisition.objects.create(
            entity=entity,
            # The requisition is where the branch sub-scope enters the chain:
            # every downstream document inherits it from here rather than
            # re-reading it from a request. An absent branch means the purchase
            # belongs to the entity as a whole.
            branch=_raised_branch(request, entity, body),
            title=str(body.get("title", "")).strip(),
            request_date=request_date,
            needed_by=needed_by,
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
    """Read requisition evidence or replace only a mutable draft estimate."""

    @property
    def rbac_permission(self):
        """Separate draft-intent edits from requisition visibility."""
        return "procurement.requisition.update" if self.request.method == "PATCH" \
            else "procurement.requisition.view"

    def get(self, request, pk):
        """Return one entity requisition with a safe workflow-instance overlay."""
        entity = resolve_entity(request)
        req = _document_or_404(
            request,
            PurchaseRequisition.objects.filter(entity=entity).select_related(
                "requested_by", "cost_center", "branch",
            ).prefetch_related("lines"),
            pk, "No such requisition in this entity.",
        )
        data = RequisitionSerializer(req).data
        # Generic workflow rows link through content type + object id; for_document builds that pair safely.
        instance = WorkflowInstance.objects.for_document(req).order_by("-created_at").first()
        data["workflow_instance_id"] = str(instance.id) if instance else None
        return success_response("Requisition retrieved.", data=data)

    @transaction.atomic
    def patch(self, request, pk):
        """Serialize draft edits; submitted and approved intent is immutable."""
        entity = resolve_entity(request)
        req = _document_or_404(
            request, PurchaseRequisition.objects.select_for_update().filter(entity=entity),
            pk, "No such requisition in this entity.",
        )
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
    """Compact, unpaginated KPI totals for the requisition list header.

    Counted over exactly the rows the caller's own list returns (same branch
    narrowing, same ``?branch=``), so the header can never report spend the
    caller is not allowed to see, nor totals that fail to reconcile with the
    rows underneath them.
    """
    rbac_permission = "procurement.requisition.view"

    def get(self, request):
        """Return same-period KPI comparisons in integer kobo for the visible rows."""
        entity = resolve_entity(request)
        as_of = timezone.localdate()
        current_start = as_of.replace(day=1)
        prior_month_end = current_start - datetime.timedelta(days=1)
        prior_start = prior_month_end.replace(day=1)
        # Compare the same number of elapsed calendar days so a partial month is not measured against a full month.
        prior_end = min(prior_month_end, prior_start + datetime.timedelta(days=as_of.day - 1))
        qs = _branch_scoped(
            request, entity, PurchaseRequisition.objects.filter(entity=entity),
            request.query_params,
        )

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
            """Return a comparable trend only when the prior denominator exists."""
            # A zero prior period has no valid percentage denominator, so the UI receives null rather than a false 0%.
            return round(((current - previous) / previous) * 100, 1) if previous else None

        return success_response("Requisition summary retrieved.", data={
            "as_of": as_of.isoformat(),
            "pending_approval": {
                "count": pending["count"], "amount": pending["amount"] or 0,
            },
            "approved_mtd": {
                "count": approved_mtd["count"], "amount": approved_mtd["amount"] or 0,
                # This tile's headline is a *count*, so its delta is an absolute
                # count change ("+2 vs prior month"), not a percentage - a % swing
                # on a small integer (0→2 = "+∞%"/"+100%") reads as noise, and a
                # zero prior month is a real comparison here, not a null one.
                "change": approved_mtd["count"] - (approved_prior["count"] or 0),
            },
            "draft": {"count": drafts["count"], "amount": drafts["amount"] or 0},
            "total_value_mtd": {
                "amount": current_total, "change_pct": percent_change(current_total, prior_total),
            },
        })


class RequisitionBudgetAvailabilityView(_ProcBase):
    """Return the approved monthly budget and open-PO commitment for a cost centre."""
    rbac_permission = "procurement.requisition.view"

    def get(self, request):
        """Compare one entity cost centre's annual approved plan with open commitments."""
        entity = resolve_entity(request)
        cost_center = _resolve_cost_center(entity, request.query_params.get("cost_center"))
        if cost_center is None:
            raise ValidationError({"cost_center": "Select a cost centre to check its budget."})
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
        fy = period.fiscal_year
        budget_amount = 0
        if budget is not None:
            # Annual allocation: the department's budgeted amount across every period
            # of the operative plan. The commitment figure below is bounded to the
            # same fiscal year so both sides of "available" share one time window
            # (previously the budget was a single month while commitments were
            # all-time, which understated availability for any department with a
            # standing open PO).
            budget_amount = BudgetLine.objects.filter(
                budget=budget, cost_center=cost_center,
            ).aggregate(total=Sum("amount"))["total"] or 0

        # Open-PO commitments for this department within the operative fiscal year.
        # Summing the PO *line's* own cost centre (not the requisition's) counts
        # directly-raised POs too, and keeps the figure correct if a line was
        # reclassified away from the requisition's department.
        committed = PurchaseOrderLine.objects.filter(
            purchase_order__entity=entity,
            purchase_order__status__in=(DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED),
            purchase_order__order_date__range=(fy.start_date, fy.end_date),
            cost_center=cost_center,
        ).aggregate(total=Sum("net_amount"))["total"] or 0
        # Available budget is the approved annual plan less commitments already raised for the department.
        available = budget_amount - committed
        return success_response("Budget availability retrieved.", data={
            "has_budget": budget is not None and budget_amount > 0,
            # Label with the operative plan (e.g. "IT CAPEX 2026") since the figures
            # are annual; a month name here would misrepresent the scope.
            "period": budget.name if budget is not None else f"FY{fy.year}",
            "budget": budget_amount,
            "committed": committed,
            "available": available,
        })


class RequisitionSubmitView(_ProcBase):
    """Submit a requisition into the ``vs_workflow`` approval engine.

    Approval is no longer a direct endpoint - submitting hands the document to its
    threshold-gated workflow template; approvers then vote through the ``vs_workflow``
    API, and the engine's callback drives the requisition to APPROVED.

    docstring-name: Submit a requisition
    """
    rbac_permission = "procurement.requisition.submit"

    def post(self, request, pk):
        """Submit entity-scoped intent; workflow owns threshold and approver routing."""
        entity = resolve_entity(request)
        req = _document_or_404(
            request, PurchaseRequisition.objects.filter(entity=entity),
            pk, "No such requisition in this entity.",
        )
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
    from vs_workflow.services import release as release_svc

    document.refresh_from_db()
    return success_response(message, data={
        "workflow_instance_id": instance.id,
        "workflow_status": instance.status,
        "approval_state": document.approval_state,
        # Tells the client in the submit response itself that nobody can approve what it
        # just submitted, so the "continue anyway" prompt costs no extra round trip.
        "approval": release_svc.approval_block(instance),
        "document": serializer_cls(document).data,
    })


class PurchaseOrderSubmitApprovalView(_ProcBase):
    """Submit a draft PO for approval without issuing or posting it."""
    rbac_permission = "procurement.purchase_order.submit"

    @transaction.atomic
    def post(self, request, pk):
        """Hand entity-scoped commitment intent to the workflow engine."""
        entity = resolve_entity(request)
        po = _document_or_404(
            request, PurchaseOrder.objects.filter(entity=entity),
            pk, "No such purchase order in this entity.",
        )
        auto_email = request.data.get("auto_email_vendor", False)
        if not isinstance(auto_email, bool):
            raise ValidationError({"auto_email_vendor": "Enter true or false."})
        delivery = None
        if auto_email:
            allowed = is_vision_super_admin(request.user) or user_has_rbac_permission(
                request.user, "procurement.purchase_order.email_vendor",
                tenant=po.entity.tenant, branch=getattr(request, "branch", None),
            )
            if not allowed:
                raise PermissionDenied("You do not have permission to email purchase orders to vendors.")
            from ..po_email import PurchaseOrderEmailError, schedule_after_approval
            try:
                delivery = schedule_after_approval(
                    po, actor_user=request.user,
                    buyer_message=request.data.get("email_message", ""),
                )
            except PurchaseOrderEmailError as exc:
                raise ValidationError({"auto_email_vendor": str(exc)}) from exc
        instance = approvals.submit_for_approval(po, actor_user=request.user)
        if delivery is not None:
            delivery.workflow_instance_id = str(instance.id)
            delivery.save(update_fields=["workflow_instance_id", "updated_at"])
        return _approval_response("Purchase order submitted for approval.",
                                  po, instance, PurchaseOrderSerializer)


class VendorInvoiceSubmitApprovalView(_ProcBase):
    """Price and match a draft bill, then submit that evidence for approval."""
    rbac_permission = "procurement.vendor_invoice.submit"

    def post(self, request, pk):
        """Freeze current match evidence for workflow; posting remains separate."""
        entity = resolve_entity(request)
        inv = _document_or_404(
            request, VendorInvoice.objects.filter(entity=entity),
            pk, "No such vendor invoice in this entity.",
        )
        if inv.status != DocumentStatus.DRAFT:
            raise ValidationError({"status": "Only a draft vendor invoice can be submitted for approval."})
        # Approval must review the same priced and matched evidence that posting
        # later revalidates under row locks.
        from .. import payables
        payables.price_vendor_invoice(inv)
        payables.match_vendor_invoice(inv, save=True)
        instance = approvals.submit_for_approval(inv, actor_user=request.user)
        return _approval_response("Vendor invoice submitted for approval.",
                                  inv, instance, VendorInvoiceSerializer)


class ApprovalTemplateSetupView(_ProcBase):
    """Provision **this tenant's own** threshold-gated approval rules.

    The rules are created for the selected entity's owning tenant, never for the
    platform: the platform-wide ladder is the shared fallback every tenant without
    its own rules reads, and one tenant's administrator must not be able to rewrite
    the threshold or the approving permissions for all the others.

    Seeded blocked on purpose: the rules arrive with nobody holding the approving
    role, so the first document submitted parks and asks for an approver to be
    appointed rather than approving itself.

    Idempotent and non-destructive: a document type whose ladder already exists is
    reported and left untouched, so re-running can never restore the defaults over a
    tenant's customised rules.

    POST body (all optional, applied only to newly created ladders): ``threshold``
    (kobo), ``manager_role_key``, ``senior_role_key``.

    docstring-name: Set up approval templates
    """
    rbac_permission = "procurement.approval.manage"

    def post(self, request):
        """Create this tenant's missing approval ladders from validated settings."""
        entity = resolve_entity(request)
        body = request.data or {}
        kwargs = {}
        if "threshold" in body:
            kwargs["threshold"] = _money(body.get("threshold"), "threshold")
        if body.get("manager_role_key"):
            kwargs["manager_role_key"] = str(body["manager_role_key"])
        if body.get("senior_role_key"):
            kwargs["senior_role_key"] = str(body["senior_role_key"])
        results = approvals.ensure_tenant_approval_templates(
            entity.tenant, created_by=request.user, **kwargs,
        )
        created = sum(1 for _, was_created in results if was_created)
        return success_response(
            "Approval rules are in place for this tenant."
            if created else "This tenant already has its own approval rules.",
            data={
                "created_count": created,
                "templates": [
                    {
                        "id": t.id, "document_type": t.document_type, "code": t.code,
                        "name": t.name, "created": was_created,
                    }
                    for t, was_created in results
                ],
            },
        )
