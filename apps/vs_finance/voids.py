"""Document-level AR void services.

Every service unwinds the authoritative sub-ledger and reverses all GL journals in
one transaction.  Allocation rows remain immutable history; document totals/statuses
are the current-state read model.
"""
from __future__ import annotations

from django.db import transaction

from .audit import record, record_rejection
from .constants import (
    CreditNoteKind,
    DocumentStatus,
    FinanceAuditAction,
    PaymentPlanStatus,
)
from .exceptions import FinanceError, PostingError
from .posting import reverse_journal


def _guard_posted(document, label):
    if document.status != DocumentStatus.POSTED or document.journal_id is None:
        raise PostingError(
            f"Only a posted {label} with a journal can be voided; "
            f"{document.document_number or document.pk} is '{document.status}'.",
        )


def _reverse(entry, owner, *, actor_user, date):
    return reverse_journal(
        entry, actor_user=actor_user, date=date, document_owner=owner,
    )


def _refuse_if_transferred(source, label):
    """Refuse to void a credit lot that funded a posted customer credit transfer.

    The transfer moved that credit to another customer, whose bills it may already
    have paid. Voiding the source first would leave the destination holding credit
    nothing in the books explains, so the transfer is voided first.
    """
    from .models import CustomerCreditTransferDraw

    lookup = {"payment": source} if label == "receipt" else {"note": source}
    draw = (
        CustomerCreditTransferDraw.objects.select_for_update()
        .filter(transfer__status=DocumentStatus.POSTED, **lookup)
        .select_related("transfer").order_by("transfer_id").first()
    )
    if draw is not None:
        raise PostingError(
            f"{label.capitalize()} {source.document_number} funded posted credit transfer "
            f"{draw.transfer.document_number}; void that transfer first.",
        )


def _refuse_if_moved(document):
    """Refuse to void a document a standing receivable move carried to another branch.

    See :func:`vs_finance.inter_branch.moved_document_refusal`: an invoice or debit
    note that moved, and a receipt or credit note whose credit a move drew on.
    """
    from .inter_branch import moved_document_refusal

    reason = moved_document_refusal(document)
    if reason is not None:
        raise PostingError(reason)


def _run_with_rejection(document, action, worker, *, actor_user, date):
    try:
        return worker(document, actor_user=actor_user, date=date)
    except FinanceError as exc:
        record_rejection(
            entity=document.entity, action=action, exc=exc,
            actor_user=actor_user, target=document,
        )
        raise


def _ensure_allocation_journal_coverage(source, links):
    """Refuse a legacy void when later-allocation GL links are incomplete."""
    ar_account_id = source.customer.receivable_account_id
    initially_applied = sum(
        int(value or 0) for value in source.journal.lines
        .filter(account_id=ar_account_id).values_list("credit", flat=True)
    )
    expected_later = max(0, int(source.allocated_amount) - initially_applied)
    linked_later = sum(int(link.amount) for link in links)
    if linked_later != expected_later:
        raise PostingError(
            f"{type(source).__name__} {source.document_number} has {expected_later} kobo "
            f"of later customer-credit allocations but only {linked_later} kobo of "
            "linked reclassification journals. Repair/backfill the allocation-journal "
            "links before voiding; reversing only part would desynchronise the ledger.",
        )


def void_invoice(invoice, *, actor_user=None, date=None):
    return _run_with_rejection(
        invoice, FinanceAuditAction.INVOICE_REVERSED, _void_invoice_atomic,
        actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_invoice_atomic(invoice, *, actor_user=None, date=None):
    from .models import Invoice, PaymentPlan

    invoice = Invoice.objects.select_for_update(of=("self",)).get(pk=invoice.pk)
    _guard_posted(invoice, "invoice")
    _refuse_if_moved(invoice)

    # A posted settlement/adjustment must be unwound through its own document first.
    # The denormalised totals also catch legacy write-offs that predate requests.
    blockers = []
    if invoice.amount_paid:
        blockers.append(f"{invoice.amount_paid} kobo of receipt settlements")
    if invoice.amount_credited:
        blockers.append(f"{invoice.amount_credited} kobo of credits/concessions/write-offs")
    posted_notes = invoice.credit_notes.filter(status=DocumentStatus.POSTED).order_by("pk")
    if posted_notes.exists():
        blockers.append("posted related credit/debit notes")
    live_plans = PaymentPlan.objects.select_for_update().filter(
        invoice=invoice,
        plan_status__in=(PaymentPlanStatus.ACTIVE, PaymentPlanStatus.COMPLETED),
    ).order_by("pk")
    if live_plans.exists():
        blockers.append("an active/completed installment plan")
    if blockers:
        raise PostingError(
            f"Invoice {invoice.document_number} cannot be voided while it has "
            f"{', '.join(blockers)}. Void/reverse those downstream documents "
            "(and cancel its installment plan) first.",
        )

    from .deferred_income import cancel_invoice_schedule
    from .deposits import cancel_invoice_deposits

    cancel_invoice_deposits(invoice)
    reversal = _reverse(invoice.journal, invoice, actor_user=actor_user, date=date)
    released_back = cancel_invoice_schedule(invoice, reversal=reversal, actor_user=actor_user)
    invoice.status = DocumentStatus.REVERSED
    invoice.save(update_fields=["status", "updated_at"])
    record(
        entity=invoice.entity, action=FinanceAuditAction.INVOICE_REVERSED,
        actor_user=actor_user, target=invoice,
        message=f"Voided invoice {invoice.document_number}.",
        journal_id=invoice.journal_id, reversal_id=reversal.pk,
        released_revenue_journal_id=getattr(released_back, "pk", None),
    )
    return invoice


def void_payment(payment, *, actor_user=None, date=None):
    return _run_with_rejection(
        payment, FinanceAuditAction.PAYMENT_REVERSED, _void_payment_atomic,
        actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_payment_atomic(payment, *, actor_user=None, date=None, transfer=None, forwarded=None):
    """Void a receipt, unwinding every settlement it made and every journal it raised.

    A credit-transfer receipt is voided only through its transfer (``transfer``),
    which also gives the credit back to the source customer; voiding the receipt on
    its own would destroy that credit. A receipt raised by an inter-branch
    transfer (money forwarded from another branch, or credit a receivable move
    carried here) is voided only through that transfer (``forwarded``), which also
    reverses the other branch's side; voiding the receipt alone would leave the
    other branch's books short.
    """
    from .installments import refresh_plans_for_invoice
    from .models import (
        CreditNote,
        CustomerCreditTransfer,
        DebitNoteAllocation,
        InterBranchTransfer,
        Invoice,
        Payment,
        PaymentAllocation,
        RefundAllocation,
    )

    payment = Payment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
    _guard_posted(payment, "receipt")
    owner = CustomerCreditTransfer.objects.filter(receipt=payment).first()
    if owner is not None and (transfer is None or transfer.pk != owner.pk):
        raise PostingError(
            f"Receipt {payment.document_number} carries credit transfer "
            f"{owner.document_number}; void the transfer instead, which returns the "
            f"credit to {owner.from_customer.code}.",
        )
    _refuse_if_transferred(payment, "receipt")
    _refuse_if_moved(payment)
    sender = InterBranchTransfer.objects.filter(receipt=payment).first()
    if sender is not None and (forwarded is None or forwarded.pk != sender.pk):
        raise PostingError(
            f"Receipt {payment.document_number} was raised by inter-branch transfer "
            f"{sender.document_number} from {sender.branch.name}; void that transfer "
            f"instead, which reverses both branches' sides.",
        )

    posted_refunds = RefundAllocation.objects.select_for_update().filter(
        payment=payment, refund__status=DocumentStatus.POSTED,
    ).select_related("refund").order_by("refund_id")
    dependent = posted_refunds.first()
    if dependent:
        raise PostingError(
            f"Receipt {payment.document_number} funded posted refund "
            f"{dependent.refund.document_number}; void that refund first.",
        )

    invoice_allocations = list(
        PaymentAllocation.objects.select_for_update().filter(payment=payment)
        .order_by("invoice_id", "pk")
    )
    debit_allocations = list(
        DebitNoteAllocation.objects.select_for_update().filter(payment=payment)
        .order_by("note_id", "pk")
    )
    invoice_ids = sorted({row.invoice_id for row in invoice_allocations})
    note_ids = sorted({row.note_id for row in debit_allocations})
    invoices = {
        obj.pk: obj for obj in Invoice.objects.select_for_update()
        .filter(pk__in=invoice_ids).order_by("pk")
    }
    notes = {
        obj.pk: obj for obj in CreditNote.objects.select_for_update()
        .filter(pk__in=note_ids).order_by("pk")
    }
    allocation_journals = list(
        payment.allocation_journals.select_for_update()
        .select_related("journal").order_by("journal__date", "journal_id")
    )
    _ensure_allocation_journal_coverage(payment, allocation_journals)

    for link in reversed(allocation_journals):
        _reverse(link.journal, payment, actor_user=actor_user, date=date)
    reversal = _reverse(payment.journal, payment, actor_user=actor_user, date=date)

    for allocation in invoice_allocations:
        invoice = invoices[allocation.invoice_id]
        invoice.amount_paid = max(0, invoice.amount_paid - allocation.amount)
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_paid", "payment_status", "updated_at"])
        refresh_plans_for_invoice(invoice, actor_user=actor_user)
    for allocation in debit_allocations:
        note = notes[allocation.note_id]
        note.amount_paid = max(0, note.amount_paid - allocation.amount)
        note.refresh_settlement_status(save=False)
        note.save(update_fields=["amount_paid", "settlement_status", "updated_at"])
    recoveries = _undo_write_off_recoveries(payment, invoices, actor_user=actor_user, date=date)

    payment.allocated_amount = 0
    payment.status = DocumentStatus.REVERSED
    payment.save(update_fields=["allocated_amount", "status", "updated_at"])
    record(
        entity=payment.entity, action=FinanceAuditAction.PAYMENT_REVERSED,
        actor_user=actor_user, target=payment,
        message=f"Voided receipt {payment.document_number}.",
        journal_id=payment.journal_id, reversal_id=reversal.pk,
        allocation_reversals=[link.journal_id for link in allocation_journals],
        write_off_recoveries_reversed=recoveries,
    )
    return payment


def _undo_write_off_recoveries(payment, invoices, *, actor_user, date):
    """Write a debt off again when the receipt that recovered it is voided.

    A recovery reinstated the written-off amount and booked recovery income on the
    strength of this receipt. With the receipt gone, the reinstatement journal is
    reversed and the amount returns to the bill as written off, so the income and
    the debt go back to where they stood before the money arrived. Returns the
    recovery ids undone.
    """
    from django.utils import timezone

    from .models import Invoice, WriteOffRecovery

    undone = []
    for recovery in (WriteOffRecovery.objects.select_for_update()
                     .filter(payment=payment, reversed_at__isnull=True)
                     .select_related("write_off", "journal").order_by("pk")):
        _reverse(recovery.journal, recovery, actor_user=actor_user, date=date)
        write_off = recovery.write_off
        invoice = invoices.get(write_off.invoice_id) or Invoice.objects.select_for_update().get(
            pk=write_off.invoice_id)
        invoice.amount_credited += recovery.amount
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
        write_off.recovered_amount = max(0, write_off.recovered_amount - recovery.amount)
        write_off.save(update_fields=["recovered_amount", "updated_at"])
        recovery.reversed_at = timezone.now()
        recovery.save(update_fields=["reversed_at", "updated_at"])
        undone.append(recovery.pk)
    return undone


def void_credit_note(note, *, actor_user=None, date=None):
    return _run_with_rejection(
        note, FinanceAuditAction.CREDIT_NOTE_REVERSED, _void_credit_note_atomic,
        actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_credit_note_atomic(note, *, actor_user=None, date=None):
    from .installments import refresh_plans_for_invoice
    from .models import (
        CreditNote,
        CreditNoteAllocation,
        DebitNoteAllocation,
        Invoice,
        RefundAllocation,
    )

    note = CreditNote.objects.select_for_update(of=("self",)).get(pk=note.pk)
    _guard_posted(note, "credit/debit note")
    _refuse_if_moved(note)

    if note.kind == CreditNoteKind.DEBIT:
        settlement = (
            DebitNoteAllocation.objects.select_for_update()
            .filter(note=note, payment__status=DocumentStatus.POSTED)
            .select_related("payment").order_by("payment_id").first()
        )
        if settlement:
            raise PostingError(
                f"Debit note {note.document_number} is settled by posted receipt "
                f"{settlement.payment.document_number}; void that receipt first.",
            )
        allocations = []
        invoices = {}
        allocation_journals = []
    else:
        _refuse_if_transferred(note, "credit note")
        refund_source = (
            RefundAllocation.objects.select_for_update()
            .filter(note=note, refund__status=DocumentStatus.POSTED)
            .select_related("refund").order_by("refund_id").first()
        )
        if refund_source:
            raise PostingError(
                f"Credit note {note.document_number} funded posted refund "
                f"{refund_source.refund.document_number}; void that refund first.",
            )
        allocations = list(
            CreditNoteAllocation.objects.select_for_update().filter(note=note)
            .order_by("invoice_id", "pk")
        )
        invoice_ids = sorted({row.invoice_id for row in allocations})
        invoices = {
            obj.pk: obj for obj in Invoice.objects.select_for_update()
            .filter(pk__in=invoice_ids).order_by("pk")
        }
        allocation_journals = list(
            note.allocation_journals.select_for_update()
            .select_related("journal").order_by("journal__date", "journal_id")
        )
        _ensure_allocation_journal_coverage(note, allocation_journals)

    for link in reversed(allocation_journals):
        _reverse(link.journal, note, actor_user=actor_user, date=date)
    reversal = _reverse(note.journal, note, actor_user=actor_user, date=date)
    from .deferred_income import restore_unwinds
    from .deposits import restore_released_deposits

    restore_unwinds(note.journal, actor_user=actor_user, date=date)  # What the note took back waits again.
    restore_released_deposits(note)  # Deposits the note returned are held again.

    for allocation in allocations:
        invoice = invoices[allocation.invoice_id]
        invoice.amount_credited = max(0, invoice.amount_credited - allocation.amount)
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
        refresh_plans_for_invoice(invoice, actor_user=actor_user)

    note.allocated_amount = 0
    note.status = DocumentStatus.REVERSED
    note.save(update_fields=["allocated_amount", "status", "updated_at"])
    record(
        entity=note.entity, action=FinanceAuditAction.CREDIT_NOTE_REVERSED,
        actor_user=actor_user, target=note,
        message=f"Voided {note.kind.lower()} note {note.document_number}.",
        journal_id=note.journal_id, reversal_id=reversal.pk,
        allocation_reversals=[link.journal_id for link in allocation_journals],
    )
    return note


def void_refund(refund, *, actor_user=None, date=None):
    return _run_with_rejection(
        refund, FinanceAuditAction.REFUND_REVERSED, _void_refund_atomic,
        actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_refund_atomic(refund, *, actor_user=None, date=None):
    from .models import CreditNote, Payment, Refund, RefundAllocation

    refund = Refund.objects.select_for_update(of=("self",)).get(pk=refund.pk)
    _guard_posted(refund, "refund")
    allocations = list(
        RefundAllocation.objects.select_for_update().filter(refund=refund)
        .order_by("payment_id", "note_id", "pk")
    )
    payment_ids = sorted({row.payment_id for row in allocations if row.payment_id})
    note_ids = sorted({row.note_id for row in allocations if row.note_id})
    payments = {
        obj.pk: obj for obj in Payment.objects.select_for_update()
        .filter(pk__in=payment_ids).order_by("pk")
    }
    notes = {
        obj.pk: obj for obj in CreditNote.objects.select_for_update()
        .filter(pk__in=note_ids).order_by("pk")
    }

    reversal = _reverse(refund.journal, refund, actor_user=actor_user, date=date)
    for allocation in allocations:
        source = payments[allocation.payment_id] if allocation.payment_id else notes[allocation.note_id]
        source.refunded_amount = max(0, source.refunded_amount - allocation.amount)
        source.save(update_fields=["refunded_amount", "updated_at"])

    refund.status = DocumentStatus.REVERSED
    refund.save(update_fields=["status", "updated_at"])
    record(
        entity=refund.entity, action=FinanceAuditAction.REFUND_REVERSED,
        actor_user=actor_user, target=refund,
        message=f"Voided refund {refund.document_number}.",
        journal_id=refund.journal_id, reversal_id=reversal.pk,
    )
    return refund


def void_concession(concession, *, actor_user=None, date=None):
    return _run_with_rejection(
        concession, FinanceAuditAction.CONCESSION_REVERSED, _void_concession_atomic,
        actor_user=actor_user, date=date,
    )


@transaction.atomic
def _void_concession_atomic(concession, *, actor_user=None, date=None):
    from .installments import refresh_plans_for_invoice
    from .models import Concession, Invoice

    concession = Concession.objects.select_for_update(of=("self",)).get(pk=concession.pk)
    _guard_posted(concession, "concession")
    invoice = Invoice.objects.select_for_update().get(pk=concession.invoice_id)

    reversal = _reverse(concession.journal, concession, actor_user=actor_user, date=date)
    from .deferred_income import restore_unwinds

    restore_unwinds(concession.journal, actor_user=actor_user, date=date)  # What it took back waits again.
    invoice.amount_credited = max(0, invoice.amount_credited - concession.amount)
    invoice.refresh_payment_status(save=False)
    invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
    refresh_plans_for_invoice(invoice, actor_user=actor_user)

    concession.status = DocumentStatus.REVERSED
    concession.save(update_fields=["status", "updated_at"])
    record(
        entity=concession.entity, action=FinanceAuditAction.CONCESSION_REVERSED,
        actor_user=actor_user, target=concession,
        message=f"Voided concession {concession.document_number}.",
        journal_id=concession.journal_id, reversal_id=reversal.pk,
    )
    return concession
