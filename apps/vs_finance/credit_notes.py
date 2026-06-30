"""AR adjustment services — credit/debit notes, refunds and bad-debt write-offs.

The companion to :mod:`vs_finance.receivables`: where that layer *bills and collects*,
this one *gives back, charges more, refunds and writes off*. Like the rest of the AR
core it is domain-neutral — it speaks only generic customers, invoices and accounts.

The postings raised here:

* **Credit note** (``Dr revenue/returns + Dr output tax, Cr AR``) — reduce a customer's
  receivable for a return, allowance or over-bill; optionally *applied* to invoices
  (a non-cash settlement that bumps :attr:`Invoice.amount_credited`).
* **Debit note** (``Dr AR, Cr revenue + Cr output tax``) — charge a customer more; a
  supplementary invoice, so never applied to reduce another invoice.
* **Refund** (``Dr AR control, Cr bank``) — hand cash back for an over-paid credit
  balance, restoring the receivable.
* **Write-off** (``Dr bad-debt expense, Cr AR control``) — concede an uncollectable
  receivable; clears the invoice's balance via ``amount_credited``.

All amounts are integer kobo; tax uses the same ``ROUND_HALF_UP`` discipline as
:mod:`vs_finance.receivables`.
"""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    BAD_DEBT_EXPENSE_CODE,
    CUSTOMER_CREDIT_CODE,
    CreditNoteKind,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from .exceptions import FinanceError, PostingError
from .posting import post_journal, resolve_period
from .receivables import _build_invoice_plan, compute_line_net, compute_tax


# --------------------------------------------------------------------------- #
# Pricing                                                                      #
# --------------------------------------------------------------------------- #

def price_credit_note(note) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the note totals.

    Idempotent: safe to call repeatedly while the note is still a draft.
    """
    from .models import CreditNoteLine

    for line in note.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            CreditNoteLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    note.recompute_totals(save=True)


# --------------------------------------------------------------------------- #
# Credit / debit note posting                                                  #
# --------------------------------------------------------------------------- #

def post_credit_note(note, *, actor_user=None, auto_allocate=False, allocations=None):
    """Price, validate and post a :class:`CreditNote`, raising its AR journal.

    For a CREDIT note, ``allocations`` (a list of ``(invoice, amount_kobo)``) — or
    ``auto_allocate`` — applies the credit to open invoices oldest-first. DEBIT notes
    increase the receivable and are never allocated.
    """
    try:
        return _post_credit_note_atomic(
            note, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations,
        )
    except FinanceError as exc:
        action = (
            FinanceAuditAction.DEBIT_NOTE_POSTED if note.kind == CreditNoteKind.DEBIT
            else FinanceAuditAction.CREDIT_NOTE_POSTED
        )
        record_rejection(
            entity=note.entity, action=action,
            exc=exc, actor_user=actor_user, target=note,
        )
        raise


@transaction.atomic
def _post_credit_note_atomic(note, *, actor_user=None, auto_allocate=False, allocations=None):
    from .models import JournalEntry, JournalLine

    if note.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Credit note {note.document_number or note.pk} is '{note.status}', "
            f"only a draft note can be posted.",
        )

    customer = note.customer
    ar_account = customer.receivable_account
    if ar_account is None:
        raise PostingError(
            f"Customer {customer.code} has no receivable (AR control) account set.",
        )

    price_credit_note(note)
    if note.total <= 0:
        raise PostingError("A credit/debit note must have a positive total to post.")

    is_debit = note.kind == CreditNoteKind.DEBIT
    period = resolve_period(note.entity, note.note_date)
    label = "Debit note" if is_debit else "Credit note"
    entry = JournalEntry.objects.create(
        entity=note.entity, branch=note.branch,
        date=note.note_date, period=period,
        source=JournalSource.SALES, currency=note.currency,
        narration=note.reason or f"{label} {note.document_number or ''}".strip(),
        reference=note.reference, created_by=actor_user,
    )

    # Group revenue + tax by account so the journal stays tidy.
    # Revenue grouped by (account, cost centre) so the cost-centre split survives into
    # the GL; tax stays aggregated by account (it's a liability, not P&L analytics).
    revenue_by_key: dict[tuple[int, int | None], int] = defaultdict(int)
    revenue_objs: dict[tuple[int, int | None], tuple] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}
    for line in note.lines.select_related(
        "revenue_account", "tax_code__collected_account", "cost_center",
    ):
        key = (line.revenue_account_id, line.cost_center_id)
        revenue_by_key[key] += line.net_amount
        revenue_objs[key] = (line.revenue_account, line.cost_center)
        if line.tax_amount:
            tax_acc = line.tax_code.collected_account if line.tax_code_id else None
            if tax_acc is None:
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no collected (output) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount
            tax_objs[tax_acc.id] = tax_acc

    line_no = 0
    if is_debit:
        # Dr AR (gross), Cr revenue + Cr output tax — a supplementary charge.
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=ar_account, debit=note.total, credit=0,
            description=f"AR: {customer.code}", line_no=line_no,
        )
        for (acc_id, cc_id), amount in revenue_by_key.items():
            if amount == 0:
                continue
            line_no += 1
            revenue_account, cost_center = revenue_objs[(acc_id, cc_id)]
            JournalLine.objects.create(
                entry=entry, account=revenue_account, debit=0, credit=amount,
                description="Revenue", cost_center=cost_center, line_no=line_no,
            )
        for acc_id, amount in tax_by_account.items():
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=tax_objs[acc_id], debit=0, credit=amount,
                description="Output tax", line_no=line_no,
            )
        applied = 0
    else:
        # Dr revenue/returns + Dr output tax — give value back. The credit settles
        # invoices (Cr AR) for the applied portion; the unapplied remainder becomes a
        # customer-credit liability (Cr 2140) so AR never carries a credit balance.
        for (acc_id, cc_id), amount in revenue_by_key.items():
            if amount == 0:
                continue
            line_no += 1
            revenue_account, cost_center = revenue_objs[(acc_id, cc_id)]
            JournalLine.objects.create(
                entry=entry, account=revenue_account, debit=amount, credit=0,
                description="Revenue / returns", cost_center=cost_center, line_no=line_no,
            )
        for acc_id, amount in tax_by_account.items():
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,
                description="Output tax reversal", line_no=line_no,
            )
        plan = _build_invoice_plan(customer, allocations) if (allocations is not None or auto_allocate) else []
        applied, _created = _apply_creditnote_subledger(note, plan, remaining=note.total)
        excess = note.total - applied
        if applied > 0:
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=ar_account, debit=0, credit=applied,
                description=f"AR: {customer.code}", line_no=line_no,
            )
        if excess > 0:
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=resolve_account(note.entity, CUSTOMER_CREDIT_CODE, label="customer credit"),
                debit=0, credit=excess, description=f"Customer credit: {customer.code}", line_no=line_no,
            )

    post_journal(entry, actor_user=actor_user)

    note.journal = entry
    note.status = DocumentStatus.POSTED
    if not is_debit:
        note.allocated_amount = applied
        note.save(update_fields=["journal", "status", "allocated_amount", "updated_at"])
    else:
        note.save(update_fields=["journal", "status", "updated_at"])

    record(
        entity=note.entity,
        action=(FinanceAuditAction.DEBIT_NOTE_POSTED if is_debit
                else FinanceAuditAction.CREDIT_NOTE_POSTED),
        actor_user=actor_user, target=note,
        message=f"Posted {label.lower()} for {customer.code} ({note.total} kobo).",
        journal_id=entry.pk, total=note.total, note_kind=note.kind,
    )
    return note


def _apply_creditnote_subledger(note, plan, *, remaining):
    """Create/extend CreditNoteAllocation rows + bump invoice ``amount_credited`` for
    the plan, capped at each invoice balance and ``remaining``. GL-agnostic — the
    caller posts the journal. Returns ``(applied_total, created_rows)``."""
    from .models import CreditNoteAllocation

    applied, created = 0, []
    for invoice, requested in plan:
        if remaining <= 0:
            break
        apply_amount = min(int(requested), invoice.balance_due, remaining)
        if apply_amount <= 0:
            continue
        alloc, _was = CreditNoteAllocation.objects.get_or_create(
            note=note, invoice=invoice, defaults={"amount": 0},
        )
        alloc.amount += apply_amount
        alloc.save(update_fields=["amount", "updated_at"])

        invoice.amount_credited += apply_amount
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
        # Keep any installment plan on this invoice in step with the new settlement.
        from .installments import refresh_plans_for_invoice
        refresh_plans_for_invoice(invoice)

        remaining -= apply_amount
        applied += apply_amount
        created.append(alloc)
    return applied, created


@transaction.atomic
def allocate_credit_note(note, *, allocations=None, actor_user=None):
    """Apply a posted CREDIT note's **stored customer credit** to invoices.

    Any unapplied portion of the note sits in the customer-credit liability (2140);
    applying it reclassifies it back to AR (``Dr customer-credit · Cr AR``) and
    settles the invoices. ``allocations`` is an optional ``[(invoice, amount)]`` plan;
    without it, open invoices are settled oldest-first.
    """
    from .models import JournalEntry, JournalLine

    if note.kind == CreditNoteKind.DEBIT:
        raise PostingError("A debit note increases the receivable; it cannot be allocated.")
    if note.status != DocumentStatus.POSTED:
        raise PostingError("Only a posted credit note can be allocated.")

    remaining = note.unallocated_amount
    if remaining <= 0:
        return []

    plan = _build_invoice_plan(note.customer, allocations)
    applied, created = _apply_creditnote_subledger(note, plan, remaining=remaining)
    if applied <= 0:
        return []

    customer = note.customer
    period = resolve_period(note.entity, note.note_date)
    entry = JournalEntry.objects.create(
        entity=note.entity, branch=note.branch,
        date=note.note_date, period=period,
        source=JournalSource.SALES, currency=note.currency,
        narration=f"Apply customer credit: {customer.code}",
        reference=note.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_account(note.entity, CUSTOMER_CREDIT_CODE, label="customer credit"),
        debit=applied, credit=0, description=f"Customer credit applied: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=customer.receivable_account, debit=0, credit=applied,
        description=f"AR: {customer.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    note.allocated_amount += applied
    note.save(update_fields=["allocated_amount", "updated_at"])

    record(
        entity=note.entity, action=FinanceAuditAction.CREDIT_NOTE_ALLOCATED,
        actor_user=actor_user, target=note,
        message=f"Applied {applied} kobo customer credit across {len(created)} invoice(s).",
        journal_id=entry.pk, allocated=note.allocated_amount, unallocated=note.unallocated_amount,
    )
    return created


# --------------------------------------------------------------------------- #
# Customer refund                                                              #
# --------------------------------------------------------------------------- #

def post_refund(refund, *, actor_user=None):
    """Post a customer :class:`Refund` (``Dr customer-credit (2140), Cr bank``).

    A refund pays out a customer's credit balance — so it draws down the
    customer-credit liability, not AR. Capped at the customer's available credit.
    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:
        return _post_refund_atomic(refund, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=refund.entity, action=FinanceAuditAction.REFUND_POSTED,
            exc=exc, actor_user=actor_user, target=refund,
        )
        raise


@transaction.atomic
def _post_refund_atomic(refund, *, actor_user=None):
    from .models import JournalEntry, JournalLine

    if refund.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Refund {refund.document_number or refund.pk} is '{refund.status}', "
            f"only a draft refund can be posted.",
        )
    if refund.amount <= 0:
        raise PostingError("A refund must have a positive amount to post.")

    customer = refund.customer

    from .receivables import customer_credit_balance
    available = customer_credit_balance(customer)
    if refund.amount > available:
        raise PostingError(
            f"Refund of {refund.amount} kobo exceeds {customer.code}'s available "
            f"credit ({available} kobo).",
        )

    deposit = refund.deposit_account or (
        refund.bank_account.gl_account if refund.bank_account_id else None
    )
    if deposit is None:
        raise PostingError("Refund has no bank/deposit account to pay from.")

    period = resolve_period(refund.entity, refund.refund_date)
    entry = JournalEntry.objects.create(
        entity=refund.entity, branch=refund.branch,
        date=refund.refund_date, period=period,
        source=JournalSource.BANK, currency=refund.currency,
        narration=refund.narration or f"Refund {refund.document_number or ''}".strip(),
        reference=refund.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_account(refund.entity, CUSTOMER_CREDIT_CODE, label="customer credit"),
        debit=refund.amount, credit=0, description=f"Refund: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=deposit, debit=0, credit=refund.amount,
        description=f"Refund paid: {customer.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    refund.journal = entry
    refund.deposit_account = deposit
    refund.status = DocumentStatus.POSTED
    refund.save(update_fields=["journal", "deposit_account", "status", "updated_at"])

    record(
        entity=refund.entity, action=FinanceAuditAction.REFUND_POSTED,
        actor_user=actor_user, target=refund,
        message=f"Refunded {refund.amount} kobo to {customer.code}.",
        journal_id=entry.pk, amount=refund.amount,
    )
    return refund


# --------------------------------------------------------------------------- #
# Bad-debt write-off                                                           #
# --------------------------------------------------------------------------- #

def write_off_invoice(invoice, *, amount=None, write_off_account=None,
                      write_off_date=None, narration="", actor_user=None):
    """Write off an uncollectable invoice balance as bad debt.

    Posts ``Dr bad-debt expense, Cr AR control`` for ``amount`` (defaulting to the
    full outstanding balance) and clears that much of the invoice via
    ``amount_credited``. ``write_off_account`` defaults to the entity's bad-debt /
    general expense account (CoA ``5300``).
    """
    try:
        return _write_off_invoice_atomic(
            invoice, amount=amount, write_off_account=write_off_account,
            write_off_date=write_off_date, narration=narration, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=invoice.entity, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,
            exc=exc, actor_user=actor_user, target=invoice,
        )
        raise


@transaction.atomic
def _write_off_invoice_atomic(invoice, *, amount=None, write_off_account=None,
                              write_off_date=None, narration="", actor_user=None):
    from .models import JournalEntry, JournalLine

    if invoice.status != DocumentStatus.POSTED:
        raise PostingError(
            f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}'; "
            f"only a posted invoice can be written off.",
        )

    balance = invoice.balance_due
    if balance <= 0:
        raise PostingError("Invoice has no outstanding balance to write off.")
    amount = balance if amount in (None, "") else int(amount)
    if amount <= 0:
        raise PostingError("Write-off amount must be positive.")
    if amount > balance:
        raise PostingError(
            f"Write-off amount ({amount} kobo) exceeds the outstanding balance "
            f"({balance} kobo).",
        )

    customer = invoice.customer
    ar_account = customer.receivable_account
    if ar_account is None:
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")

    expense = write_off_account or resolve_account(
        invoice.entity, BAD_DEBT_EXPENSE_CODE, label="bad-debt expense",
    )
    when = write_off_date or invoice.invoice_date
    period = resolve_period(invoice.entity, when)
    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch,
        date=when, period=period, source=JournalSource.SALES,
        narration=narration or f"Write-off {invoice.document_number or ''}".strip(),
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=expense, debit=amount, credit=0,
        description=f"Bad debt: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=ar_account, debit=0, credit=amount,
        description=f"AR write-off: {customer.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    invoice.amount_credited += amount
    invoice.refresh_payment_status(save=False)
    invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
    # A write-off reduces the outstanding balance, so any installment plan tracks it too.
    from .installments import refresh_plans_for_invoice
    refresh_plans_for_invoice(invoice, actor_user=actor_user)

    record(
        entity=invoice.entity, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,
        actor_user=actor_user, target=invoice,
        message=f"Wrote off {amount} kobo of invoice {invoice.document_number} "
                f"for {customer.code}.",
        journal_id=entry.pk, amount=amount, balance_after=invoice.balance_due,
        narration=narration or "", customer_code=customer.code, customer_name=customer.name,
    )
    return entry
