"""Purchase requisitions and approval submission.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import approvals
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
        qs = PurchaseRequisition.objects.filter(entity=entity).prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return self.paginate(request, qs.order_by("-id"), RequisitionSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        req = PurchaseRequisition.objects.create(
            entity=entity,
            request_date=_date(body.get("request_date"), "request_date", required=True),
            needed_by=_date(body.get("needed_by"), "needed_by"),
            justification=body.get("justification", ""),
            requested_by=request.user if request.user.is_authenticated else None,
            created_by=request.user if request.user.is_authenticated else None,
        )
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
                requisition=req, line_no=ln.get("line_no", i),
                description=ln.get("description") or defaults.get("description", ""),
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                estimated_unit_price=_money(unit_price or 0, "estimated_unit_price"),
                expense_account=expense, tax_code=tax,
            )
        req.recompute_total(save=True)
        return success_response(
            "Requisition created.", data=RequisitionSerializer(req).data, status=201,
        )


class RequisitionDetailView(_ProcBase):
    """docstring-name: Requisitions"""
    rbac_permission = "procurement.requisition.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        return success_response("Requisition retrieved.", data=RequisitionSerializer(req).data)


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


