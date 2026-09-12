"""vs_workflow handlers for procurement spend approvals.

Registering a handler per document type is what lets the generic ``vs_workflow`` engine
drive a requisition / purchase order / vendor invoice through approval without knowing
anything about procurement. The engine calls :meth:`resolve_default_template_code` to
pick the template, :meth:`get_document_summary` to snapshot the approval screen, and the
``on_*`` lifecycle callbacks on terminal decisions - which delegate to
:mod:`vs_procurement.approvals` to flip ``approval_state`` and apply the document-type
effect.

These handlers are imported (and thus registered) from ``VsProcurementConfig.ready``.
"""
from __future__ import annotations

from urllib.parse import urlencode

from vs_finance.constants import DocumentStatus
from vs_finance.money import format_naira
from vs_workflow.constants import DocumentAudience
from vs_workflow.exceptions import ReversalNotAllowedError
from vs_workflow.handlers import BaseWorkflowHandler, register_handler

from . import approvals
from .purchasing import goods_arrived
from .constants import (
    CLOSED_PO_STATUSES,
    WF_DEFAULT_TEMPLATE_CODE,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
    WF_DOCTYPE_VENDOR_PAYMENT,
)


#: Ledger statuses a document may still be in when its approval is undone.
#: Approval is the permission to post, so a document outside this set has already
#: reached the ledger. The set names what approval leaves behind rather than what
#: posting produces, so a document type whose posting service lands somewhere
#: unexpected fails closed instead of being reversed after its entry exists.
UNPOSTED_STATUSES = frozenset({
    DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL,
})


def _posted_block_reason(document, noun: str, remedy: str) -> str | None:
    """Refuse the reversal of an approval a posting has already acted on.

    Shared by the two payable types, whose approval is the permission to post:
    once the journal exists the money has moved in the books, and withdrawing the
    approval record behind it would leave that entry standing with nothing on
    record saying it was ever authorised.
    """
    if document.status in UNPOSTED_STATUSES:
        return None
    return (
        f"This {noun} is {document.get_status_display().lower()} and no longer "
        f"waiting on that decision, so its approval cannot be undone. {remedy}"
    )


class _ProcApprovalHandler(BaseWorkflowHandler):
    """Shared behaviour for the procurement approval handlers.

    Subclasses only declare their ``document_model``; everything else - template
    resolution, the approval-screen summary, and the terminal callbacks - is uniform
    because each document exposes ``workflow_amount_field`` and ``approval_state``.
    """

    # Schools and the platform both buy, so every procurement document is raised by all of them.
    audience = DocumentAudience.ALL

    #: Built into the summary subtitle ("Requisition", "Purchase order", …).
    noun = "Document"
    source_path = ""

    def resolve_default_template_code(self, document) -> str:
        return WF_DEFAULT_TEMPLATE_CODE

    def get_source_document_link(self, document) -> str:
        query = urlencode({"document": document.pk, "entity": document.entity.code})
        return f"{self.source_path}?{query}"

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
            "link": self.get_source_document_link(document),
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

    # --- reversal ----------------------------------------------------------- #
    def reversal_block_reason(self, document) -> str | None:
        """Why this document's approval can no longer be undone, or ``None``.

        Each document type answers for its own, because what approval releases
        differs by type and only the owning type knows it: a requisition becomes
        an order, an order reaches a vendor, a bill and a payment reach the
        ledger. A type with nothing to say here is one whose approval has left
        nothing behind that a reversal cannot take back.
        """
        return None

    def validate_reversal(self, instance, context) -> None:
        """Refuse a reversal whose decision has already had an effect in the world.

        The engine can withdraw its own record of a vote; it cannot withdraw what
        the vote released. Asking every type through
        :meth:`reversal_block_reason` and raising the refusal here in one shape is
        what stops one type being asked while the others reverse silently with
        their effect still standing.
        """
        document = instance.document
        reason = None if document is None else self.reversal_block_reason(document)
        if reason is None:
            return None
        raise ReversalNotAllowedError(
            reason, document_number=document.document_number or str(document.pk),
        )

    def on_action_reversed(self, instance, context) -> None:
        """Put the document back in the approval queue it was decided out of."""
        approvals.reset_to_pending(instance.document)


@register_handler(WF_DOCTYPE_REQUISITION)
class RequisitionApprovalHandler(_ProcApprovalHandler):
    noun = "Purchase requisition"
    source_path = "/procurement/requisitions"

    @property
    def document_model(self):
        from .models import PurchaseRequisition
        return PurchaseRequisition

    def reversal_block_reason(self, document) -> str | None:
        """Refuse while an order raised from this requisition stands, or took delivery.

        Approval is what lets a buyer raise a purchase order, so the question is
        what those orders did, never whether one was ever raised. Goods answer
        first and answer whatever state their order is in now: stock on the shelf
        and the GR/IR liability that came with it are not handed back by
        cancelling the order they arrived against, so the approval that let them
        in cannot be withdrawn either, and saying so is more use than telling
        somebody to cancel an order that is already cancelled. Failing that, an
        order still live is a commitment to a vendor that holds whether or not the
        vote behind it does, and it is stopped by cancelling the order, where the
        vendor is told. When every order raised has been cancelled and nothing
        arrived, this approval has released nothing that outlives it, which is the
        case a reversal exists for.
        """
        from .models import GoodsReceivedNote

        if goods_arrived(GoodsReceivedNote.objects.filter(
                purchase_order__requisition=document)):
            return (
                "Goods have already been received against a purchase order raised "
                "from this requisition, so its approval cannot be undone."
            )
        if document.purchase_orders.exclude(status__in=CLOSED_PO_STATUSES).exists():
            return (
                "A purchase order has already been raised from this requisition, "
                "so its approval cannot be undone. Cancel the order instead."
            )
        return None


@register_handler(WF_DOCTYPE_PURCHASE_ORDER)
class PurchaseOrderApprovalHandler(_ProcApprovalHandler):
    noun = "Purchase order"
    source_path = "/procurement/purchase-orders"

    @property
    def document_model(self):
        from .models import PurchaseOrder
        return PurchaseOrder

    def reversal_block_reason(self, document) -> str | None:
        """Refuse once the order has reached the vendor or its goods have arrived.

        Approving a purchase order releases its email to the supplier, and a
        supplier who has read "your order is approved" is already acting on it.
        Undoing the approval record afterwards changes nothing they can see, so
        the order is stopped by cancelling it, where the vendor is told.

        A posted receipt is the same fact reached another way: an order whose
        email never went out can still have been placed by telephone, and once the
        goods are received the stock and the GR/IR liability both exist.
        """
        from .constants import PurchaseOrderVendorDeliveryStatus
        from .models import PurchaseOrderVendorDelivery

        released = PurchaseOrderVendorDelivery.objects.filter(
            purchase_order=document,
            status__in=(PurchaseOrderVendorDeliveryStatus.PENDING,
                        PurchaseOrderVendorDeliveryStatus.SENT),
        ).exists()
        if released:
            return (
                "This purchase order has already gone to the vendor, so its "
                "approval cannot be undone. Cancel the order instead."
            )
        if goods_arrived(document.goods_receipts):
            return (
                "Goods have already been received against this purchase order, "
                "so its approval cannot be undone."
            )
        return None


@register_handler(WF_DOCTYPE_VENDOR_INVOICE)
class VendorInvoiceApprovalHandler(_ProcApprovalHandler):
    noun = "Vendor invoice"
    source_path = "/procurement/vendor-invoices"

    @property
    def document_model(self):
        from .models import VendorInvoice
        return VendorInvoice

    def reversal_block_reason(self, document) -> str | None:
        """Refuse once the bill has posted, because approval is what let it post."""
        return _posted_block_reason(
            document, "bill",
            "A posted bill is corrected with a reversing entry, not by editing "
            "the approval behind it.",
        )


@register_handler(WF_DOCTYPE_VENDOR_PAYMENT)
class VendorPaymentApprovalHandler(_ProcApprovalHandler):
    noun = "Vendor payment"
    source_path = "/procurement/vendor-payments"

    @property
    def document_model(self):
        from .models import VendorPayment
        return VendorPayment

    def reversal_block_reason(self, document) -> str | None:
        """Refuse once the payment has posted, because the money has left."""
        return _posted_block_reason(
            document, "payment",
            "A posted payment is corrected by reversing the payment, not by "
            "editing the approval behind it.",
        )
