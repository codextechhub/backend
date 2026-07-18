"""vs_workflow handlers for procurement spend approvals.

Registering a handler per document type is what lets the generic ``vs_workflow`` engine
drive a requisition / purchase order / vendor invoice through approval without knowing
anything about procurement. The engine calls :meth:`resolve_default_template_code` to
pick the template, :meth:`get_document_summary` to snapshot the approval screen, and the
``on_*`` lifecycle callbacks on terminal decisions — which delegate to
:mod:`vs_procurement.approvals` to flip ``approval_state`` and apply the document-type
effect.

These handlers are imported (and thus registered) from ``VsProcurementConfig.ready``.
"""
from __future__ import annotations

from vs_finance.money import format_naira
from vs_workflow.handlers import BaseWorkflowHandler, register_handler

from . import approvals
from .constants import (
    WF_DEFAULT_TEMPLATE_CODE,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
    WF_DOCTYPE_VENDOR_PAYMENT,
)


class _ProcApprovalHandler(BaseWorkflowHandler):
    """Shared behaviour for the procurement approval handlers.

    Subclasses only declare their ``document_model``; everything else — template
    resolution, the approval-screen summary, and the terminal callbacks — is uniform
    because each document exposes ``workflow_amount_field`` and ``approval_state``.
    """

    #: Built into the summary subtitle ("Requisition", "Purchase order", …).
    noun = "Document"

    def resolve_default_template_code(self, document) -> str:
        return WF_DEFAULT_TEMPLATE_CODE

    def get_document_summary(self, document) -> dict:
        amount = getattr(document, document.workflow_amount_field, 0) or 0
        vendor = getattr(document, "vendor", None)
        fields = [{"label": "Amount", "value": format_naira(amount)}]
        if vendor is not None:
            fields.append({"label": "Vendor", "value": f"{vendor.code} · {vendor.name}"})
        return {
            "title": document.document_number or str(document.pk),
            "subtitle": self.noun,
            "fields": fields,
        }

    # --- terminal callbacks ------------------------------------------------- #
    def on_approved(self, instance, context) -> None:
        approvals.apply_approved(instance.document, actor_user=instance.requested_by)

    def on_rejected(self, instance, context) -> None:
        approvals.apply_rejected(
            instance.document, reason=context.get("comment", ""),
            actor_user=instance.requested_by,
        )

    def on_withdrawn(self, instance, context) -> None:
        approvals.reset_pending(instance.document)

    def on_cancelled(self, instance, context) -> None:
        approvals.reset_pending(instance.document)


@register_handler(WF_DOCTYPE_REQUISITION)
class RequisitionApprovalHandler(_ProcApprovalHandler):
    noun = "Purchase requisition"

    @property
    def document_model(self):
        from .models import PurchaseRequisition
        return PurchaseRequisition


@register_handler(WF_DOCTYPE_PURCHASE_ORDER)
class PurchaseOrderApprovalHandler(_ProcApprovalHandler):
    noun = "Purchase order"

    @property
    def document_model(self):
        from .models import PurchaseOrder
        return PurchaseOrder


@register_handler(WF_DOCTYPE_VENDOR_INVOICE)
class VendorInvoiceApprovalHandler(_ProcApprovalHandler):
    noun = "Vendor invoice"

    @property
    def document_model(self):
        from .models import VendorInvoice
        return VendorInvoice


@register_handler(WF_DOCTYPE_VENDOR_PAYMENT)
class VendorPaymentApprovalHandler(_ProcApprovalHandler):
    noun = "Vendor payment"

    @property
    def document_model(self):
        from .models import VendorPayment
        return VendorPayment
