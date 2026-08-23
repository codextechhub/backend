"""Customer-facing finance notifications, routed through vs_notifications.

Delivery of AR lifecycle events - an invoice or account-adjustment note was issued, a
receipt was recorded - to the customer's billing email. Everything goes through the
platform notification system; vs_finance never sends email itself.

Invoices and receipts delegate to :mod:`vs_finance.document_email`, which records a
:class:`~vs_finance.models.FinanceDocumentDelivery` for the automatic copy, attaches a
PDF, and makes the send re-sendable and retryable. Credit and debit notes still send
directly from here: they carry no attached document and have no re-send control, so a
delivery row would record something nobody can act on.

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

from .constants import CreditNoteKind
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

    Delegates to :mod:`vs_finance.document_email` so the automatic copy is recorded
    as a :class:`~vs_finance.models.FinanceDocumentDelivery` alongside any later
    re-send. Sending it from here without a delivery row is what previously made the
    history start at the first manual send and read as though nothing went out on
    posting. Opening-balance invoices are still skipped, and this still never raises.
    """
    from .document_email import issue_invoice_copy

    delivery = issue_invoice_copy(invoice, actor_user=actor_user)
    return list(delivery.notification_ids) if delivery is not None else None


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

    Fires for every posted customer receipt (manual and gateway), through
    :mod:`vs_finance.document_email` so the copy is recorded and can be re-sent or
    retried. Returns the notification ids, which the Celery task reports back as its
    queued/not-queued result.
    """
    from .document_email import issue_receipt_copy

    delivery = issue_receipt_copy(payment, actor_user=actor_user)
    return list(delivery.notification_ids) if delivery is not None else None
