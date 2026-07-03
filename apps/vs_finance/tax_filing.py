"""Tax-remittance / filing services — drain statutory liability control accounts.

The platform's source transactions already park what is owed to the authorities in
liability control accounts: sales credit **Output VAT** (2200), purchases debit recoverable
**Input VAT** (1300), vendor payments credit **WHT Payable** (2300), and payroll credits
**PAYE** (2310) and **Pension** (2320). A :class:`~vs_finance.models.TaxObligation` names
one such control account, its authority and filing cadence; a
:class:`~vs_finance.models.TaxFiling` is one return over one period.

Three moments, mirroring the ``DRAFT → FILED → PAID`` lifecycle:

* **Prepare** (:func:`prepare_filing`) — derive the amount owed straight from the GL: the
  net credit movement of the liability account over the period (for VAT, less the
  recoverable input movement). A draft worksheet; nothing posts.
* **File** (:func:`file_filing`) — freeze the figures and submit. Posts a journal **only**
  to net recoverable input VAT off the output payable and/or to add a penalty/interest
  adjustment, leaving the liability account holding exactly ``amount_due``.
* **Pay** (:func:`pay_filing`) — ``Dr liability, Cr bank`` for the remittance; partial
  payments supported.

:func:`outstanding_obligations` is a read-only view of what each obligation currently owes.

All amounts are integer kobo; every mutating call records a durable rejection audit on a
:class:`FinanceError` and re-raises.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Sum

from .audit import record, record_rejection
from .constants import (
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
    TaxFilingStatus,
)
from .exceptions import FinanceError, TaxFilingError
from .posting import post_journal, resolve_period


# --------------------------------------------------------------------------- #
# GL movement helper                                                           #
# --------------------------------------------------------------------------- #

def _account_movement(entity, account, *, period_start=None, period_end=None):
    """Return ``(debit_sum, credit_sum)`` of POSTED journal lines for ``account``.

    Bounded to ``[period_start, period_end]`` when given; otherwise the all-time running
    movement (i.e. the account's current balance components).
    """
    from .constants import DocumentStatus
    from .models import JournalLine

    qs = JournalLine.objects.filter(
        account=account,
        entry__entity=entity,
        entry__status=DocumentStatus.POSTED,
    )
    if period_start is not None:
        qs = qs.filter(entry__date__gte=period_start)
    if period_end is not None:
        qs = qs.filter(entry__date__lte=period_end)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return int(agg["d"] or 0), int(agg["c"] or 0)


# --------------------------------------------------------------------------- #
# Prepare (derive amount due from the GL — no posting)                         #
# --------------------------------------------------------------------------- #

def prepare_filing(obligation, *, period_start, period_end, due_date=None,
                   currency=None, actor_user=None):
    """Create (or refresh) a DRAFT :class:`TaxFiling` with the amount owed read from the GL.

    Re-running for the same obligation/period updates the existing draft rather than
    duplicating it. Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:
        return _prepare_filing_atomic(
            obligation, period_start=period_start, period_end=period_end,
            due_date=due_date, currency=currency, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=obligation.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=obligation,
        )
        raise


@transaction.atomic
def _prepare_filing_atomic(obligation, *, period_start, period_end, due_date,
                           currency, actor_user):
    from .models import TaxFiling

    if not obligation.is_active:
        raise TaxFilingError(f"Tax obligation '{obligation.code}' is inactive.")
    if period_end < period_start:
        raise TaxFilingError("The filing period end cannot precede its start.")

    entity = obligation.entity
    debit, credit = _account_movement(
        entity, obligation.liability_account,
        period_start=period_start, period_end=period_end,
    )
    gross = max(credit - debit, 0)  # credit-normal payable accrued in the period

    recoverable = 0
    if obligation.recoverable_account_id:
        rdebit, rcredit = _account_movement(
            entity, obligation.recoverable_account,
            period_start=period_start, period_end=period_end,
        )
        # Input tax is a debit-normal asset; never net below a zero remittance.
        recoverable = min(max(rdebit - rcredit, 0), gross)

    filing = TaxFiling.objects.filter(
        entity=entity, obligation=obligation,
        period_start=period_start, period_end=period_end,
        filing_status=TaxFilingStatus.DRAFT,
    ).first()
    if filing is None:
        # A new draft must not straddle any other filing for the same obligation.
        # Overlap: existing.period_start <= new.period_end AND existing.period_end >= new.period_start.
        # The exact-match draft above is the refresh path and is excluded here.
        clash = (
            TaxFiling.objects.filter(
                entity=entity, obligation=obligation,
                period_start__lte=period_end, period_end__gte=period_start,
            )
            .exclude(
                period_start=period_start, period_end=period_end,
                filing_status=TaxFilingStatus.DRAFT,
            )
            .order_by("period_start")
            .first()
        )
        if clash is not None:
            raise TaxFilingError(
                f"Filing period {period_start}–{period_end} overlaps existing "
                f"{obligation.code} filing {clash.document_number or clash.pk} "
                f"({clash.period_start}–{clash.period_end}).",
            )
        filing = TaxFiling(
            entity=entity, obligation=obligation,
            period_start=period_start, period_end=period_end,
        )
    filing.due_date = due_date
    filing.currency = currency or filing.currency
    filing.gross_liability = gross
    filing.recoverable_amount = recoverable
    filing.adjustment_amount = int(filing.adjustment_amount or 0)
    filing.recompute_due(save=False)
    filing.save()

    record(
        entity=entity, action=FinanceAuditAction.TAX_FILING_PREPARED,
        actor_user=actor_user, target=filing,
        message=(
            f"Prepared {obligation.code} filing for {period_start}–{period_end}: "
            f"{filing.amount_due} kobo due."
        ),
        total=filing.amount_due, tax=recoverable,
    )
    return filing


# --------------------------------------------------------------------------- #
# File (freeze figures; net input VAT + book any penalty)                      #
# --------------------------------------------------------------------------- #

def file_filing(filing, *, filed_date, filing_reference="", adjustment_amount=0,
                adjustment_account=None, actor_user=None):
    """Submit a draft return: freeze figures, net recoverable input VAT, book any penalty.

    ``adjustment_amount`` (kobo) is a late-filing penalty / interest added to the amount
    due, posted ``Dr adjustment_account (expense), Cr liability``. Records a durable
    rejection audit on any :class:`FinanceError`.
    """
    try:
        return _file_filing_atomic(
            filing, filed_date=filed_date, filing_reference=filing_reference,
            adjustment_amount=adjustment_amount, adjustment_account=adjustment_account,
            actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


@transaction.atomic
def _file_filing_atomic(filing, *, filed_date, filing_reference, adjustment_amount,
                        adjustment_account, actor_user):
    from .models import JournalEntry, JournalLine

    if filing.filing_status != TaxFilingStatus.DRAFT:
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is '{filing.filing_status}', "
            f"only a draft can be filed.",
        )

    adjustment_amount = int(adjustment_amount or 0)
    if adjustment_amount < 0:
        raise TaxFilingError("A penalty/interest adjustment cannot be negative.")
    if adjustment_amount and adjustment_account is None:
        raise TaxFilingError("A penalty/interest adjustment needs an expense account.")

    filing.adjustment_amount = adjustment_amount
    filing.adjustment_account = adjustment_account
    filing.recompute_due(save=False)
    if filing.amount_due <= 0:
        raise TaxFilingError("Nothing to file — the computed amount due is zero.")

    obligation = filing.obligation
    recoverable = int(filing.recoverable_amount)

    entry = None
    if recoverable > 0 or adjustment_amount > 0:
        period = resolve_period(filing.entity, filed_date)
        entry = JournalEntry.objects.create(
            entity=filing.entity, branch=filing.branch, date=filed_date, period=period,
            source=JournalSource.CLOSING, currency=filing.currency,
            narration=f"Tax filing {filing.document_number or ''}: {obligation.code}".strip(),
            created_by=actor_user,
        )
        line_no = 0
        if recoverable > 0:
            # Net recoverable input VAT off the output payable.
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=obligation.liability_account,
                debit=recoverable, credit=0,
                description="Net input VAT against output", line_no=line_no,
            )
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=obligation.recoverable_account,
                debit=0, credit=recoverable,
                description="Clear recoverable input VAT", line_no=line_no,
            )
        if adjustment_amount > 0:
            # Penalty / interest increases the payable.
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=adjustment_account,
                debit=adjustment_amount, credit=0,
                description="Tax penalty / interest", line_no=line_no,
            )
            line_no += 1
            JournalLine.objects.create(
                entry=entry, account=obligation.liability_account,
                debit=0, credit=adjustment_amount,
                description="Penalty added to payable", line_no=line_no,
            )
        post_journal(entry, actor_user=actor_user)
        filing.filing_journal = entry

    filing.filing_reference = filing_reference
    filing.filed_at = filed_date
    filing.filing_status = TaxFilingStatus.FILED
    filing.save()

    record(
        entity=filing.entity, action=FinanceAuditAction.TAX_FILING_FILED,
        actor_user=actor_user, target=filing,
        message=(
            f"Filed {obligation.code} return {filing.document_number or filing.pk} "
            f"({filing.amount_due} kobo due)."
        ),
        journal_id=entry.pk if entry else None,
        total=filing.amount_due, tax=filing.recoverable_amount,
    )
    return filing


# --------------------------------------------------------------------------- #
# Unfile (revert a FILED return to DRAFT — the audit-correct undo)             #
# --------------------------------------------------------------------------- #

def unfile_filing(filing, *, actor_user=None):
    """Revert a FILED return to DRAFT: reverse its netting/penalty journal, clear filing.

    The undo for a return filed in error, before any remittance. A PAID return is
    refused (reverse the remittance first); so is a return with any payment recorded.
    Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:
        return _unfile_filing_atomic(filing, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


@transaction.atomic
def _unfile_filing_atomic(filing, *, actor_user=None):
    from .posting import reverse_journal

    if filing.filing_status == TaxFilingStatus.PAID:
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is PAID; reverse the "
            f"remittance before un-filing it.",
        )
    if filing.filing_status != TaxFilingStatus.FILED:
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is '{filing.filing_status}', "
            f"only a filed return can be un-filed.",
        )
    if int(filing.amount_paid or 0) > 0:
        raise TaxFilingError(
            "This filing carries a remittance; reverse the payment before un-filing it.",
        )

    reversed_journal_id = None
    if filing.filing_journal_id is not None:
        reverse_journal(filing.filing_journal, actor_user=actor_user)
        reversed_journal_id = filing.filing_journal_id
        filing.filing_journal = None

    filing.filed_at = None
    filing.filing_reference = ""
    filing.filing_status = TaxFilingStatus.DRAFT
    filing.save(update_fields=[
        "filing_journal", "filed_at", "filing_reference", "filing_status", "updated_at",
    ])

    record(
        entity=filing.entity, action=FinanceAuditAction.TAX_FILING_UNFILED,
        actor_user=actor_user, target=filing,
        message=(
            f"Un-filed {filing.obligation.code} return "
            f"{filing.document_number or filing.pk} back to draft."
        ),
        journal_id=reversed_journal_id,
    )
    return filing


# --------------------------------------------------------------------------- #
# Pay / remit (Dr liability, Cr bank)                                          #
# --------------------------------------------------------------------------- #

def pay_filing(filing, *, bank_account, pay_date, amount=None, actor_user=None):
    """Remit a filed return: ``Dr liability, Cr bank``.

    ``amount`` defaults to the full outstanding balance; a smaller amount records a
    partial remittance. Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:
        return _pay_filing_atomic(
            filing, bank_account=bank_account, pay_date=pay_date,
            amount=amount, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


@transaction.atomic
def _pay_filing_atomic(filing, *, bank_account, pay_date, amount, actor_user):
    from .models import JournalEntry, JournalLine

    if filing.filing_status not in (TaxFilingStatus.FILED, TaxFilingStatus.PAID):
        raise TaxFilingError("Only a filed return can be remitted.")
    if bank_account.entity_id != filing.entity_id:
        raise TaxFilingError("The bank account belongs to a different entity.")
    outstanding = filing.balance_due
    if outstanding <= 0:
        raise TaxFilingError("This filing has no outstanding balance to remit.")
    pay = outstanding if amount is None else min(int(amount), outstanding)
    if pay <= 0:
        raise TaxFilingError("Remittance amount must be positive.")

    obligation = filing.obligation
    period = resolve_period(filing.entity, pay_date)
    entry = JournalEntry.objects.create(
        entity=filing.entity, branch=filing.branch, date=pay_date, period=period,
        source=JournalSource.BANK, currency=filing.currency,
        narration=f"Remit {obligation.code} {filing.document_number or ''}".strip(),
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=obligation.liability_account, debit=pay, credit=0,
        description=f"{obligation.code} remitted to {obligation.authority_name or 'authority'}",
        line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=pay,
        description="Tax remittance paid", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    filing.amount_paid = int(filing.amount_paid) + pay
    filing.refresh_payment_status(save=False)
    if filing.payment_status == InvoicePaymentStatus.PAID:
        filing.filing_status = TaxFilingStatus.PAID
    filing.save(update_fields=[
        "amount_paid", "payment_status", "filing_status", "updated_at",
    ])

    record(
        entity=filing.entity, action=FinanceAuditAction.TAX_FILING_PAID,
        actor_user=actor_user, target=filing,
        message=(
            f"Remitted {pay} kobo of {obligation.code} filing "
            f"{filing.document_number or filing.pk}."
        ),
        journal_id=entry.pk, amount=pay, payment_status=filing.payment_status,
    )
    return filing


# --------------------------------------------------------------------------- #
# Read-only — what each obligation currently owes                              #
# --------------------------------------------------------------------------- #

def outstanding_obligations(entity) -> list:
    """Per-obligation snapshot of the unremitted balance sitting in each control account.

    The running net credit balance of each active obligation's ``liability_account`` (less
    any recoverable input balance), i.e. what would be owed if a return were filed for all
    activity to date. Returns one dict per active obligation.
    """
    from .models import TaxObligation

    rows = []
    qs = (
        TaxObligation.objects
        .filter(entity=entity, is_active=True)
        .select_related("liability_account", "recoverable_account")
        .order_by("code")
    )
    for ob in qs:
        debit, credit = _account_movement(entity, ob.liability_account)
        payable = credit - debit
        recoverable = 0
        if ob.recoverable_account_id:
            rdebit, rcredit = _account_movement(entity, ob.recoverable_account)
            recoverable = rdebit - rcredit
        net = max(payable - max(recoverable, 0), 0)
        rows.append({
            "obligation_id": ob.id, "code": ob.code, "name": ob.name,
            "obligation_type": ob.obligation_type,
            "authority_name": ob.authority_name,
            "liability_code": ob.liability_account.code,
            "payable_balance": payable,
            "recoverable_balance": recoverable,
            "net_outstanding": net,
        })
    return rows
