"""Customer-facing finance notifications, routed through vs_notifications.

Fire-and-forget delivery of AR lifecycle events - an invoice or account-adjustment
note was issued, a receipt was recorded - to the customer's billing email. Everything
goes through the platform notification system; vs_finance never sends email itself.

Delivery is **best-effort**. These run on the *success path* of a money-posting
service (:func:`vs_finance.receivables.post_invoice` / ``post_payment``), so a
notification problem - a misconfigured/inactive event, a missing template, or the
notifications app being absent - must never raise back into the posting and roll the
ledger back. Every entry point swallows and logs its own errors.

Notifications are **recipient-centric** (per the vs_notifications overhaul): the
customer's billing email is the recipient (an ``UnregisteredRecipient`` - a payer
need not have a portal account), and the school (from ``entity.tenant.school_profile``) is an
*optional scope*, not a gate - platform/product books deliver just the same.
"""
from __future__ import annotations

import logging

from .constants import CreditNoteKind, InvoiceSource
from .money import to_naira

logger = logging.getLogger(__name__)  # Module logger for notification failures.


# Format integer kobo for notification templates.
def _naira(kobo) -> str:
    """Thousands-separated naira string, no symbol - the templates prepend ₦."""
    return f"{to_naira(int(kobo or 0)):,.2f}"  # Normalize missing values to zero and format with commas.


# Format a signed net balance as an unambiguous account position.
def _account_position(kobo: int) -> tuple[str, str]:
    """Return ``(label, absolute amount)`` for a signed customer net balance.

    Positive means the customer owes; negative means the customer has credit. Keeping
    the label beside an unsigned amount prevents a minus sign in an email from being
    misread as either an amount due or a credit.
    """
    value = int(kobo or 0)
    if value < 0:
        return "Credit balance available", _naira(abs(value))
    return "Amount outstanding", _naira(value)


# Send best-effort invoice-issued notification.
def notify_invoice_issued(invoice, *, actor_user=None):
    """Best-effort: email the customer that an invoice was issued.

    Skips opening-balance invoices (``source == OPENING``) - those are migration
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
            "due_date": invoice.due_date.isoformat() if invoice.due_date else "-",  # ISO due date or dash.
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
    except Exception:  # best-effort - never break the posting
        logger.warning(  # Log failure with stack trace for operations.
            "invoice_issued notification failed for invoice %s",  # Include invoice primary key.
            getattr(invoice, "pk", None), exc_info=True,  # Avoid attribute errors while logging.
        )
        return None  # Swallow failures so ledger posting remains committed.


# Send best-effort credit/debit-note issuance notification.
def notify_credit_note_issued(note, *, actor_user=None):
    """Best-effort: tell the customer that a credit/debit note changed their account.

    The debit and credit directions use separate event keys and templates so the
    customer cannot confuse an additional charge with a reduction. The current net
    account position includes invoices, debit notes and available customer credit.
    Never raises: financial posting remains authoritative even if delivery fails.
    """
    try:  # Notification failures must never roll back note posting.
        from vs_notifications.notify import send_notification, UnregisteredRecipient

        from .views_ar import _customer_ledger

        customer = note.customer
        school = getattr(note.entity.tenant, "school_profile", None)
        ledger = _customer_ledger(note.entity, [customer.id]).get(customer.id, {})
        current_net = int(ledger.get("outstanding", 0)) - int(ledger.get("credit", 0))
        is_debit = note.kind == CreditNoteKind.DEBIT
        # Every debit note increases net AR by its total; every credit note reduces it
        # by its total whether it was applied to an invoice or held as customer credit.
        previous_net = current_net - note.total if is_debit else current_net + note.total
        previous_label, previous_amount = _account_position(previous_net)
        current_label, current_amount = _account_position(current_net)
        issuer_name = school.name if school else note.entity.name
        related_invoice = note.invoice.document_number if note.invoice_id else "Standalone account adjustment"
        event_key = "billing.debit_note_issued" if is_debit else "billing.credit_note_issued"
        direction = {
            "badge": "ADDITIONAL CHARGE" if is_debit else "ACCOUNT CREDIT",
            "title": "A debit note has been added to your account" if is_debit
                     else "A credit note has been applied to your account",
            "summary": (
                "This additional charge increases your account balance. It is payable "
                "alongside your other open billing documents."
                if is_debit else
                "This adjustment reduces the amount you owe. If it exceeds your open "
                "charges, the remainder stays available as account credit."
            ),
            "amount_label": "Additional amount charged" if is_debit else "Amount credited",
            "action_title": "What you need to do" if is_debit else "What this means",
            "action_message": (
                f"Please include debit note {note.document_number} when reviewing or "
                "paying your outstanding account balance."
                if is_debit else
                f"No payment is required for credit note {note.document_number}. "
                "Use the updated account position shown above when making your next payment."
            ),
            "accent_color": "#b54708" if is_debit else "#067647",
            "accent_soft": "#fffaeb" if is_debit else "#ecfdf3",
            "accent_border": "#fedf89" if is_debit else "#abefc6",
        }

        context = {
            "customer_name": customer.name,
            "note_number": note.document_number,
            "note_date": note.note_date.isoformat() if note.note_date else "-",
            "note_amount": _naira(note.total),
            "reason": note.reason or "Account adjustment",
            "related_invoice": related_invoice,
            "previous_balance_label": previous_label,
            "previous_balance_amount": previous_amount,
            "current_balance_label": current_label,
            "current_balance_amount": current_amount,
            "issuer_name": issuer_name,
            **direction,
        }
        return send_notification(
            event_key=event_key,
            context=context,
            recipients=[],
            school=school,
            unregistered_recipients=[
                UnregisteredRecipient(email=customer.billing_email or "", name=customer.name),
            ],
            metadata={
                "finance_document_type": "DEBIT_NOTE" if is_debit else "CREDIT_NOTE",
                "finance_document_id": note.pk,
                "document_number": note.document_number,
            },
        )
    except Exception:  # best-effort - never break the posting
        logger.warning(
            "credit_note_issued notification failed for note %s",
            getattr(note, "pk", None), exc_info=True,
        )
        return None


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
            "payment_date": payment.payment_date.isoformat() if payment.payment_date else "-",  # ISO date or dash.
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
    except Exception:  # best-effort - never break the posting
        logger.warning(  # Log failure with stack trace for operations.
            "payment_received notification failed for payment %s",  # Include payment primary key.
            getattr(payment, "pk", None), exc_info=True,  # Avoid attribute errors while logging.
        )
        return None  # Swallow failures so ledger posting remains committed.
