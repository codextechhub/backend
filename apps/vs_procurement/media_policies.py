"""Who may read a procurement attachment through ``/media/``.

Vendor paper - quotations, supplier invoices, payment receipts - is commercially
sensitive within a school as well as between schools: which vendor quoted what,
and what the school actually paid. Each attachment therefore demands the same
verb as the document it hangs off, scoped to that document's branch, so an
attachment is never readable by someone who would be refused its parent.

Registered from :meth:`VsProcurementConfig.ready`, so ``core`` never imports
procurement to find out.
"""
from __future__ import annotations

from core.media import register_policy
from vs_rbac.evaluator import has_permission

from .models import (
    VendorInvoiceAttachment,
    VendorPaymentAttachment,
    VendorQuotationAttachment,
)


def _document_policy(key: str, document_of):
    def _predicate(request, row) -> bool:
        document = document_of(row)
        if document is None:
            return False
        return has_permission(
            request.user, key,
            tenant=getattr(document.entity, "tenant", None),
            branch=getattr(document, "branch", None),
        )
    return _predicate


def register() -> None:
    register_policy(
        VendorQuotationAttachment,
        # A quotation belongs to the RFQ that solicited it; the RFQ is what
        # carries the entity and branch, and "may read this RFQ" is the right
        # question for a vendor's own paper against it.
        _document_policy("procurement.rfq.view", lambda row: row.quotation.rfq),
    )
    register_policy(
        VendorInvoiceAttachment,
        _document_policy("procurement.vendor_invoice.view", lambda row: row.vendor_invoice),
    )
    register_policy(
        VendorPaymentAttachment,
        _document_policy("procurement.vendor_payment.view", lambda row: row.payment),
    )
