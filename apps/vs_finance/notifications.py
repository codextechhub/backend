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
need not have a portal account), and the school (``entity.source_school``) is an
*optional scope*, not a gate — platform/product books deliver just the same.
"""
from __future__ import annotations

import logging

from .constants import InvoiceSource
from .money import to_naira

logger = logging.getLogger(__name__)


def _naira(kobo) -> str:
    """Thousands-separated naira string, no symbol — the templates prepend ₦."""
    return f"{to_naira(int(kobo or 0)):,.2f}"


def notify_invoice_issued(invoice, *, actor_user=None):
    """Best-effort: email the customer that an invoice was issued.

    Skips opening-balance invoices (``source == OPENING``) — those are migration
    artefacts, not real charges the customer should be emailed about. Never raises:
    a delivery problem must not roll back the invoice posting.
    """
    try:
        if invoice.source == InvoiceSource.OPENING:
            return None

        from django.conf import settings
        from vs_notifications.notify import send_notification, UnregisteredRecipient

        customer = invoice.customer
        school = invoice.entity.source_school  # optional scope; may be None
        context = {
            "customer_name": customer.name,
            "invoice_number": invoice.document_number,
            "invoice_amount": _naira(invoice.total),
            "due_date": invoice.due_date.isoformat() if invoice.due_date else "—",
            "school_name": school.name if school else "",
            # No standing hosted pay page yet; fall back to the configured callback.
            "payment_link": getattr(settings, "PAYMENTS_CALLBACK_URL", "") or "",
        }
        return send_notification(
            event_key="billing.invoice_issued",
            context=context,
            recipients=[],
            school=school,
            unregistered_recipients=[
                UnregisteredRecipient(email=customer.billing_email or "", name=customer.name),
            ],
        )
    except Exception:  # best-effort — never break the posting
        logger.warning(
            "invoice_issued notification failed for invoice %s",
            getattr(invoice, "pk", None), exc_info=True,
        )
        return None


def notify_payment_received(payment, *, actor_user=None):
    """Best-effort: email the customer that a receipt was recorded. Never raises.

    Fires for every posted customer receipt (manual and gateway). When the receipt
    settled specific invoices, the first allocated invoice number is surfaced for the
    template; otherwise it renders blank.
    """
    try:
        from vs_notifications.notify import send_notification, UnregisteredRecipient

        customer = payment.customer
        school = payment.entity.source_school  # optional scope; may be None
        alloc = payment.allocations.select_related("invoice").first()
        invoice_number = alloc.invoice.document_number if alloc is not None else ""
        context = {
            "customer_name": customer.name,
            "invoice_number": invoice_number,
            "amount_paid": _naira(payment.amount),
            "payment_date": payment.payment_date.isoformat() if payment.payment_date else "—",
            "receipt_number": payment.document_number,
            "school_name": school.name if school else "",
        }
        return send_notification(
            event_key="billing.payment_received",
            context=context,
            recipients=[],
            school=school,
            unregistered_recipients=[
                UnregisteredRecipient(email=customer.billing_email or "", name=customer.name),
            ],
        )
    except Exception:  # best-effort — never break the posting
        logger.warning(
            "payment_received notification failed for payment %s",
            getattr(payment, "pk", None), exc_info=True,
        )
        return None
