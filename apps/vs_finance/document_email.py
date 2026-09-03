"""Emailing a finance document to a customer: recipients, PDF, queueing, outcome.

Every customer-facing email vs_finance sends goes through here, whether it was raised
automatically by a posting or asked for by a person. That single path is the point:
before it existed, :mod:`vs_finance.notifications` called ``send_notification`` and
kept nothing, so the system could not say whether a customer had ever been emailed,
could not re-send a document on request, and could not retry a failed delivery.

vs_finance still sends no email itself. It renders the document, hands the message to
:mod:`vs_notifications`, and records what happened in
:class:`~vs_finance.models.FinanceDocumentDelivery`. The notification app owns SMTP,
per-attempt retries and channel settings; this module owns the finance-side story.

Two callers with deliberately different failure behaviour:

* **Automatic** (``issue_*_copy``) runs on the success path of a posting, so it never
  raises. A ledger entry must not roll back because a mail server was unreachable.
  The delivery row still records the failure, and it can be retried afterwards.
* **Manual** (``send_*``) is a user pressing a button and must fail loudly, so the
  screen can say why nothing was sent.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .audit import record
from .constants import (
    FinanceAuditAction,
    FinanceAuditStatus,
    FinanceDeliveryDocument,
    FinanceDeliverySource,
    FinanceDeliveryStatus,
)
from .exceptions import FinanceError
from .models import FinanceDocumentDelivery
from .pay_links import invoice_pay_url

logger = logging.getLogger(__name__)

MAX_NOTE_LENGTH = 1000

# Which notification event carries each document. Invoice and receipt reuse the
# events the automatic posting emails already use - a re-send is the same message,
# not a new kind of message - so their templates stay in one place.
EVENT_KEYS = {
    FinanceDeliveryDocument.INVOICE: "billing.invoice_issued",
    FinanceDeliveryDocument.RECEIPT: "billing.payment_received",
    FinanceDeliveryDocument.STATEMENT: "billing.statement_issued",
}


class FinanceDocumentEmailError(FinanceError):
    error_code = "DOCUMENT_EMAIL_ERROR"
    default_message = "The document could not be emailed."
    http_status = 400


# --------------------------------------------------------------------------- #
# Recipients                                                                  #
# --------------------------------------------------------------------------- #

def _dedupe(values) -> list[str]:
    result, seen = [], set()
    for value in values:
        email = str(value or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            result.append(email)
    return result


def resolve_recipients(customer) -> list[str]:
    """Where a customer's documents go. One billing email today, a list by contract.

    Returning a list rather than a string is not speculative generality: the
    notification layer takes a list, procurement already resolves several vendor
    contacts, and customer contacts are the obvious next step. Callers that assume a
    single address would all have to change then.
    """
    return _dedupe([getattr(customer, "billing_email", "")])


def finance_bcc(recipients=None) -> list[str]:
    """The finance monitoring copy, minus anyone already a direct recipient.

    Blind, not visible: the customer has no reason to see our internal mailbox,
    and a visible copy makes reply-all a route into it.
    """
    direct = {value.lower() for value in recipients or []}
    return [
        value for value in _dedupe(getattr(settings, "FINANCE_CUSTOMER_EMAIL_BCC", []))
        if value not in direct
    ]


def _clean_note(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FinanceDocumentEmailError("The note must be text.")
    value = value.strip()
    if len(value) > MAX_NOTE_LENGTH:
        raise FinanceDocumentEmailError(f"Use {MAX_NOTE_LENGTH:,} characters or fewer for the note.")
    return value


def _issuer_name(entity) -> str:
    school = getattr(entity.tenant, "school_profile", None)
    return school.name if school is not None else entity.name


def _subject(document_type, entity, document_number) -> str:
    issuer = _issuer_name(entity)
    if document_type == FinanceDeliveryDocument.INVOICE:
        return f"Invoice {document_number} from {issuer}"
    if document_type == FinanceDeliveryDocument.RECEIPT:
        return f"Receipt {document_number} from {issuer}"
    return f"Statement of account from {issuer}"


def preview(*, customer, document_type, entity, document_number="") -> dict:
    """What a send would do, so nobody emails a customer without seeing the address."""
    recipients = resolve_recipients(customer)
    return {
        "recipients": recipients,
        "bcc": finance_bcc(recipients),
        "subject": _subject(document_type, entity, document_number),
        "can_send": bool(recipients),
        "blocked_reason": "" if recipients else
                          "This customer has no billing email. Add one on the customer record first.",
    }


# --------------------------------------------------------------------------- #
# Audit                                                                       #
# --------------------------------------------------------------------------- #

def _audit(delivery, action, message, *, actor_user=None, status=FinanceAuditStatus.SUCCESS):
    return record(
        entity=delivery.entity,
        action=action,
        actor_user=actor_user or delivery.requested_by,
        target_type=f"{delivery.document_type.title()}Delivery",
        target_id=str(delivery.pk),
        document_number=delivery.document_number,
        status=status,
        message=message,
        delivery_id=delivery.pk,
        delivery_source=delivery.source,
        document_type=delivery.document_type,
        recipients=delivery.recipients,
        bcc=delivery.bcc,
        notification_ids=delivery.notification_ids,
    )


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def _render(delivery):
    """Build the PDF and the template context for one delivery.

    Loading the document here, rather than passing the object through from the
    caller, is what lets a retry reproduce a delivery from its stored row alone.
    """
    document_type = delivery.document_type
    customer = delivery.customer

    if document_type == FinanceDeliveryDocument.INVOICE:
        from .models import Invoice
        from .pdf import invoice_pdf

        invoice = Invoice.objects.select_related(
            "customer", "entity__tenant", "branch",
        ).prefetch_related("lines__tax_code", "lines__cost_center", "lines__revenue_account").get(pk=delivery.document_id)
        return invoice_pdf(invoice, note=delivery.note), _invoice_context(invoice, delivery), \
            f"Invoice-{invoice.document_number}.pdf"

    if document_type == FinanceDeliveryDocument.RECEIPT:
        from .models import Payment
        from .pdf import receipt_pdf

        payment = Payment.objects.select_related(
            "customer", "entity__tenant", "branch",
        ).prefetch_related("allocations__invoice").get(pk=delivery.document_id)
        return receipt_pdf(payment, note=delivery.note), _receipt_context(payment, delivery), \
            f"Receipt-{payment.document_number}.pdf"

    from .pdf import statement_pdf

    pdf = statement_pdf(
        customer, start_date=delivery.period_start, end_date=delivery.period_end, note=delivery.note,
    )
    return pdf, _statement_context(customer, delivery), f"Statement-{customer.code}.pdf"


def _naira(kobo) -> str:
    from .money import to_naira

    return f"{to_naira(int(kobo or 0)):,.2f}"


def _invoice_context(invoice, delivery) -> dict:
    return {
        "customer_name": invoice.customer.name,
        "invoice_number": invoice.document_number,
        "invoice_amount": _naira(invoice.total),
        "amount_outstanding": _naira(invoice.balance_due),
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "-",
        "school_name": _issuer_name(invoice.entity),
        "issuer_name": _issuer_name(invoice.entity),
        # The public pay page for this invoice: not a checkout URL, and not the
        # post-payment return URL, which lands the customer on a "thanks for
        # paying" screen they have not paid on. The page mints the checkout
        # when they click, so the amount is whatever is still owed then.
        "payment_link": invoice_pay_url(invoice),
        "note": delivery.note,
    }


def _receipt_context(payment, delivery) -> dict:
    allocation = payment.allocations.select_related("invoice").first()
    return {
        "customer_name": payment.customer.name,
        "invoice_number": allocation.invoice.document_number if allocation is not None else "",
        "amount_paid": _naira(payment.amount),
        "payment_date": payment.payment_date.isoformat() if payment.payment_date else "-",
        "receipt_number": payment.document_number,
        "school_name": _issuer_name(payment.entity),
        "issuer_name": _issuer_name(payment.entity),
        "note": delivery.note,
    }


def _statement_context(customer, delivery) -> dict:
    from .money import format_naira
    from .reports import customer_statement

    statement = customer_statement(
        customer, start_date=delivery.period_start, end_date=delivery.period_end,
    )
    return {
        "customer_name": customer.name,
        "issuer_name": _issuer_name(customer.entity),
        "school_name": _issuer_name(customer.entity),
        "period_start": str(statement.start_date) if statement.start_date else "inception",
        "period_end": str(statement.end_date),
        "opening_balance": format_naira(statement.opening_balance),
        "closing_balance": format_naira(statement.closing_balance),
        "total_charges": format_naira(statement.total_debits),
        "total_payments": format_naira(statement.total_credits),
        "entry_count": len(statement.entries),
        "note": delivery.note,
    }


# --------------------------------------------------------------------------- #
# Queueing                                                                    #
# --------------------------------------------------------------------------- #

@transaction.atomic
def _queue(delivery_id: int, *, actor_user=None):
    """Render, attach and hand one delivery to vs_notifications."""
    from vs_notifications.notify import UnregisteredRecipient, send_notification

    delivery = FinanceDocumentDelivery.objects.select_for_update(of=("self",)).select_related(
        "customer", "entity__tenant", "requested_by",
    ).get(pk=delivery_id)

    # A crash between queueing and committing could otherwise send twice.
    if delivery.status != FinanceDeliveryStatus.PENDING or delivery.notification_ids:
        return delivery
    if not delivery.recipients:
        raise FinanceDocumentEmailError(
            "This customer has no billing email. Add one on the customer record first."
        )

    pdf, context, filename = _render(delivery)
    delivery.pdf_file.save(filename, ContentFile(pdf), save=False)
    delivery.queued_at = timezone.now()
    delivery.failure_reason = ""
    delivery.save(update_fields=["pdf_file", "queued_at", "failure_reason", "updated_at"])

    school = getattr(delivery.entity.tenant, "school_profile", None)
    notification_ids = send_notification(
        event_key=EVENT_KEYS[delivery.document_type],
        context=context,
        recipients=[],
        school=school,
        unregistered_recipients=[
            UnregisteredRecipient(email=email, name=delivery.customer.name)
            for email in delivery.recipients
        ],
        metadata={
            "finance_delivery_id": delivery.pk,
            "finance_document_type": delivery.document_type,
            "document_number": delivery.document_number,
            "bcc": delivery.bcc,
            "attachments": [{
                "name": filename,
                "storage_name": delivery.pdf_file.name,
                "content_type": "application/pdf",
            }],
        },
    )

    delivery.notification_ids = notification_ids or []
    if not notification_ids:
        # Every channel disabled, or dispatch declined it. Nothing is in flight, so
        # nothing will ever report back - record the outcome now rather than leaving
        # the row pending for ever.
        delivery.status = FinanceDeliveryStatus.FAILED
        delivery.failure_reason = "No email notification could be queued."
    delivery.save(update_fields=["notification_ids", "status", "failure_reason", "updated_at"])

    _audit(
        delivery,
        FinanceAuditAction.DOCUMENT_EMAIL_QUEUED if notification_ids
        else FinanceAuditAction.DOCUMENT_EMAIL_FAILED,
        f"Queued {delivery.get_document_type_display().lower()} "
        f"{delivery.document_number or delivery.customer.code} for "
        f"{len(delivery.recipients)} recipient(s)." if notification_ids else
        f"Could not queue {delivery.get_document_type_display().lower()} "
        f"{delivery.document_number or delivery.customer.code} for email.",
        actor_user=actor_user,
        status=FinanceAuditStatus.SUCCESS if notification_ids else FinanceAuditStatus.FAILED,
    )
    return delivery


def _create(*, entity, customer, document_type, document_id="", document_number="",
            period_start=None, period_end=None, source, actor_user, note="", parent=None):
    recipients = resolve_recipients(customer)
    if not recipients:
        raise FinanceDocumentEmailError(
            "This customer has no billing email. Add one on the customer record first."
        )
    return FinanceDocumentDelivery.objects.create(
        entity=entity,
        customer=customer,
        document_type=document_type,
        document_id=str(document_id or ""),
        document_number=document_number or "",
        period_start=period_start,
        period_end=period_end,
        source=source,
        status=FinanceDeliveryStatus.PENDING,
        requested_by=actor_user,
        parent=parent,
        note=_clean_note(note),
        recipients=recipients,
        bcc=finance_bcc(recipients),
    )


# --------------------------------------------------------------------------- #
# Manual sends - these raise, so a screen can explain the failure              #
# --------------------------------------------------------------------------- #

@transaction.atomic
def send_invoice(invoice, *, actor_user, note="", source=FinanceDeliverySource.MANUAL, parent=None):
    """Email a posted invoice to its customer."""
    from .constants import DocumentStatus

    if invoice.status != DocumentStatus.POSTED:
        raise FinanceDocumentEmailError("Only a posted invoice can be emailed to a customer.")
    delivery = _create(
        entity=invoice.entity, customer=invoice.customer,
        document_type=FinanceDeliveryDocument.INVOICE,
        document_id=invoice.pk, document_number=invoice.document_number,
        source=source, actor_user=actor_user, note=note, parent=parent,
    )
    return _queue(delivery.pk, actor_user=actor_user)


@transaction.atomic
def send_receipt(payment, *, actor_user, note="", source=FinanceDeliverySource.MANUAL, parent=None):
    """Email a posted receipt to its customer."""
    from .constants import DocumentStatus

    if payment.status != DocumentStatus.POSTED:
        raise FinanceDocumentEmailError("Only a posted receipt can be emailed to a customer.")
    delivery = _create(
        entity=payment.entity, customer=payment.customer,
        document_type=FinanceDeliveryDocument.RECEIPT,
        document_id=payment.pk, document_number=payment.document_number,
        source=source, actor_user=actor_user, note=note, parent=parent,
    )
    return _queue(delivery.pk, actor_user=actor_user)


@transaction.atomic
def send_statement(customer, *, actor_user, start_date=None, end_date=None, note="",
                   source=FinanceDeliverySource.MANUAL, parent=None):
    """Email a statement of account for one customer over a period."""
    if start_date and end_date and start_date > end_date:
        raise FinanceDocumentEmailError("The statement period ends before it starts.")
    delivery = _create(
        entity=customer.entity, customer=customer,
        document_type=FinanceDeliveryDocument.STATEMENT,
        period_start=start_date, period_end=end_date or timezone.now().date(),
        source=source, actor_user=actor_user, note=note, parent=parent,
    )
    return _queue(delivery.pk, actor_user=actor_user)


@transaction.atomic
def retry(delivery, *, actor_user, note=None):
    """Try a failed delivery again as a new attempt, keeping the original visible."""
    if delivery.status != FinanceDeliveryStatus.FAILED:
        raise FinanceDocumentEmailError("Only a failed delivery can be retried.")
    message = delivery.note if note is None else note
    new_delivery = _create(
        entity=delivery.entity, customer=delivery.customer,
        document_type=delivery.document_type,
        document_id=delivery.document_id, document_number=delivery.document_number,
        period_start=delivery.period_start, period_end=delivery.period_end,
        source=FinanceDeliverySource.RETRY, actor_user=actor_user,
        note=message, parent=delivery,
    )
    return _queue(new_delivery.pk, actor_user=actor_user)


# --------------------------------------------------------------------------- #
# Automatic sends - these never raise                                         #
# --------------------------------------------------------------------------- #

def _best_effort(fn, *, what, pk):
    try:
        return fn()
    except Exception:
        logger.warning("%s email failed for %s", what, pk, exc_info=True)
        return None


def issue_invoice_copy(invoice, *, actor_user=None):
    """Best-effort automatic copy when an invoice posts. Never raises."""
    from .constants import InvoiceSource

    # Opening balances are migration artefacts, not charges a customer should hear about.
    if invoice.source == InvoiceSource.OPENING:
        return None
    return _best_effort(
        lambda: send_invoice(
            invoice, actor_user=actor_user, source=FinanceDeliverySource.AUTOMATIC,
        ),
        what="invoice_issued", pk=getattr(invoice, "pk", None),
    )


def issue_receipt_copy(payment, *, actor_user=None):
    """Best-effort automatic copy when a receipt posts. Never raises."""
    return _best_effort(
        lambda: send_receipt(
            payment, actor_user=actor_user, source=FinanceDeliverySource.AUTOMATIC,
        ),
        what="payment_received", pk=getattr(payment, "pk", None),
    )


# --------------------------------------------------------------------------- #
# Outcome                                                                     #
# --------------------------------------------------------------------------- #

@transaction.atomic
def update_from_notification(notification, *, success: bool):
    """Settle a delivery once every notification it raised has reported back."""
    from vs_notifications.constants import ChannelChoices
    from vs_notifications.models import Notification, NotificationStatus

    delivery_id = (notification.metadata or {}).get("finance_delivery_id")
    if not delivery_id:
        return
    delivery = FinanceDocumentDelivery.objects.select_for_update(of=("self",)).filter(
        pk=delivery_id,
    ).select_related("entity", "customer").first()
    if delivery is None or delivery.status not in (
        FinanceDeliveryStatus.PENDING, FinanceDeliveryStatus.FAILED,
    ):
        return

    # Judge the delivery by its EMAIL notifications only. These events also declare
    # an in-app channel, so dispatch raises an in-app row alongside the email - one
    # with no recipient user, because the customer is an unregistered payer with no
    # console account. Nobody can read that row, so letting it decide whether a
    # document "reached" the customer would be wrong in both directions: an in-app
    # failure would report a delivered document as failed, and its instant success
    # could mask a bounced email.
    email_notifications = Notification.objects.filter(
        id__in=delivery.notification_ids, channel=ChannelChoices.EMAIL,
    )
    statuses = list(email_notifications.values_list("status", flat=True))
    # Still in flight: wait for the last recipient rather than reporting early.
    if not statuses or any(value == NotificationStatus.PENDING for value in statuses):
        return

    label = delivery.get_document_type_display().lower()
    reference = delivery.document_number or delivery.customer.code
    if all(value == NotificationStatus.SENT for value in statuses):
        delivery.status = FinanceDeliveryStatus.SENT
        delivery.sent_at = timezone.now()
        delivery.failure_reason = ""
        action, audit_status = FinanceAuditAction.DOCUMENT_EMAIL_SENT, FinanceAuditStatus.SUCCESS
        message = f"Sent {label} {reference} to {delivery.customer.name}."
    else:
        failures = email_notifications.filter(
            status=NotificationStatus.FAILED,
        ).values_list("failure_reason", flat=True)
        delivery.status = FinanceDeliveryStatus.FAILED
        delivery.failure_reason = "; ".join(v for v in failures if v)[:2000] or "Email delivery failed."
        action, audit_status = FinanceAuditAction.DOCUMENT_EMAIL_FAILED, FinanceAuditStatus.FAILED
        message = f"Could not send {label} {reference} to {delivery.customer.name}."

    delivery.save(update_fields=["status", "sent_at", "failure_reason", "updated_at"])
    _audit(delivery, action, message, status=audit_status)
