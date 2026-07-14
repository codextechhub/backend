"""Customer-facing finance notifications, routed through vs_notifications.

Fire-and-forget delivery of AR lifecycle events — an invoice was issued, a receipt
was recorded — to the customer's billing email. Everything goes through the platform
notification system; vs_finance never sends email itself.

Delivery is **best-effort**. These run on the *success path* of a money-posting
service (:func:`vs_finance.receivables.post_invoice` / ``post_payment``), so a
notification problem — a misconfigured/inactive event, a missing template, or the
notifications app being absent — must never raise back into the posting and roll the
ledger back. Every entry point swallows and logs its own errors.

Notifications are **recipient-centric** (per the vs_notifications overhaul): the
customer's billing email is the recipient (an ``UnregisteredRecipient`` — a payer
need not have a portal account), and the school (from ``entity.tenant.school_profile``) is an
*optional scope*, not a gate — platform/product books deliver just the same.
"""
from __future__ import annotations

import logging

from .constants import InvoiceSource
from .money import to_naira

logger = logging.getLogger(__name__)  # Module logger for notification failures.


# Format integer kobo for notification templates.
def _naira(kobo) -> str:
    """Thousands-separated naira string, no symbol — the templates prepend ₦."""
    return f"{to_naira(int(kobo or 0)):,.2f}"  # Normalize missing values to zero and format with commas.


# Send best-effort invoice-issued notification.
def notify_invoice_issued(invoice, *, actor_user=None):
    """Best-effort: email the customer that an invoice was issued.

    Skips opening-balance invoices (``source == OPENING``) — those are migration
    artefacts, not real charges the customer should be emailed about. Never raises:
    a delivery problem must not roll back the invoice posting.
    """
    try:  # Notification failures must never roll back invoice posting.
        if invoice.source == InvoiceSource.OPENING:  # Opening balance invoices are migration artefacts.
            return None

        from django.conf import settings
        from vs_notifications.notify import send_notification, UnregisteredRecipient

        customer = invoice.customer  # Recipient information comes from the invoice customer.
        school = getattr(invoice.entity.tenant, "school_profile", None)  # optional scope; may be None.
        context = {  # Template variables for the invoice-issued event.
            "customer_name": customer.name,  # Customer display name.
            "invoice_number": invoice.document_number,  # Posted invoice document number.
            "invoice_amount": _naira(invoice.total),  # Human-readable invoice total.
            "due_date": invoice.due_date.isoformat() if invoice.due_date else "—",  # ISO due date or dash.
            "school_name": school.name if school else "",  # Optional school name.
            # No standing hosted pay page yet; fall back to the configured callback.  # Keep link configurable.
            "payment_link": getattr(settings, "PAYMENTS_CALLBACK_URL", "") or "",  # Optional payment URL.
        }
        return send_notification(  # Delegate delivery to vs_notifications.
            event_key="billing.invoice_issued",  # Event key configured in notification templates.
            context=context,  # Render data for the notification template.
            recipients=[],  # No registered portal recipients are targeted here.
            school=school,  # Optional school scoping for notification configuration.
            unregistered_recipients=[  # Billing emails can receive without portal accounts.
                UnregisteredRecipient(email=customer.billing_email or "", name=customer.name),  # Customer email/name payload.
            ],
        )
    except Exception:  # best-effort — never break the posting
        logger.warning(  # Log failure with stack trace for operations.
            "invoice_issued notification failed for invoice %s",  # Include invoice primary key.
            getattr(invoice, "pk", None), exc_info=True,  # Avoid attribute errors while logging.
        )
        return None  # Swallow failures so ledger posting remains committed.


# Send best-effort payment-received notification.
def notify_payment_received(payment, *, actor_user=None):
    """Best-effort: email the customer that a receipt was recorded. Never raises.

    Fires for every posted customer receipt (manual and gateway). When the receipt
    settled specific invoices, the first allocated invoice number is surfaced for the
    template; otherwise it renders blank.
    """
    try:  # Notification failures must never roll back payment posting.
        from vs_notifications.notify import send_notification, UnregisteredRecipient

        customer = payment.customer  # Recipient information comes from the payment customer.
        school = getattr(payment.entity.tenant, "school_profile", None)  # optional scope; may be None.
        alloc = payment.allocations.select_related("invoice").first()
        invoice_number = alloc.invoice.document_number if alloc is not None else ""  # Surface one settled invoice number.
        context = {  # Template variables for the payment-received event.
            "customer_name": customer.name,  # Customer display name.
            "invoice_number": invoice_number,  # First allocated invoice number, if available.
            "amount_paid": _naira(payment.amount),  # Human-readable receipt amount.
            "payment_date": payment.payment_date.isoformat() if payment.payment_date else "—",  # ISO date or dash.
            "receipt_number": payment.document_number,  # Posted receipt document number.
            "school_name": school.name if school else "",  # Optional school name.
        }
        return send_notification(  # Delegate delivery to vs_notifications.
            event_key="billing.payment_received",  # Event key configured in notification templates.
            context=context,  # Render data for the notification template.
            recipients=[],  # No registered portal recipients are targeted here.
            school=school,  # Optional school scoping for notification configuration.
            unregistered_recipients=[  # Billing emails can receive without portal accounts.
                UnregisteredRecipient(email=customer.billing_email or "", name=customer.name),  # Customer email/name payload.
            ],
        )
    except Exception:  # best-effort — never break the posting
        logger.warning(  # Log failure with stack trace for operations.
            "payment_received notification failed for payment %s",  # Include payment primary key.
            getattr(payment, "pk", None), exc_info=True,  # Avoid attribute errors while logging.
        )
        return None  # Swallow failures so ledger posting remains committed.
