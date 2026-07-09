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
from __future__ import annotations  # Defer annotation evaluation during app import.

import datetime  # Date arithmetic for due dates and filing periods.

from django.db import transaction  # Keeps tax filing mutations atomic.
from django.db.models import Sum  # Aggregates posted journal line movements.

from .audit import record, record_rejection  # Finance audit helpers.
from .constants import (
    FinanceAuditAction,  # Audit action enum values.
    InvoicePaymentStatus,  # Paid/partial/unpaid status enum reused for remittance status.
    JournalSource,  # Journal source enum values.
    TaxFilingStatus,  # Tax filing lifecycle statuses.
)
from .exceptions import FinanceError, TaxFilingError  # Base finance and tax filing errors.
from .posting import post_journal, resolve_period  # GL posting and period resolution helpers.


# --------------------------------------------------------------------------- #
# GL movement helper                                                           #
# --------------------------------------------------------------------------- #

def _default_due_date(period_end, filing_day):  # Compute default statutory due date.
    """Day ``filing_day`` of the month *after* ``period_end``, clamped to that month's length.

    Matches the obligation's ``filing_day`` help_text ("Day of the month after period end
    the return is due"). A small local month-arithmetic helper (deliberately not imported
    from :mod:`assets`, to keep the tax service self-contained).
    """
    year = period_end.year + (1 if period_end.month == 12 else 0)  # Move to next year after December.
    month = 1 if period_end.month == 12 else period_end.month + 1  # Month after period end.
    if month == 12:  # December's next month is next January.
        next_month_first = datetime.date(year + 1, 1, 1)  # First day after due month.
    else:  # Other due months advance within the same year.
        next_month_first = datetime.date(year, month + 1, 1)  # First day of following month.
    last_day = (next_month_first - datetime.timedelta(days=1)).day  # Last valid due-month day.
    return datetime.date(year, month, min(int(filing_day), last_day))  # Clamp due day to month length.


def _account_movement(entity, account, *, period_start=None, period_end=None):  # Sum posted GL movement for one account.
    """Return ``(debit_sum, credit_sum)`` of POSTED journal lines for ``account``.

    Bounded to ``[period_start, period_end]`` when given; otherwise the all-time running
    movement (i.e. the account's current balance components).
    """
    from .constants import DocumentStatus  # Posted journal status enum.
    from .models import JournalLine  # Journal line model containing debit/credit amounts.

    qs = JournalLine.objects.filter(  # Base posted movement query.
        account=account,  # Account being measured.
        entry__entity=entity,  # Scope to entity.
        entry__status=DocumentStatus.POSTED,  # Only posted journals affect balances.
    )
    if period_start is not None:  # Optional lower date bound.
        qs = qs.filter(entry__date__gte=period_start)  # Include entries on/after start.
    if period_end is not None:  # Optional upper date bound.
        qs = qs.filter(entry__date__lte=period_end)  # Include entries on/before end.
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))  # Sum debit and credit movements.
    return int(agg["d"] or 0), int(agg["c"] or 0)  # Return integer kobo totals.


# --------------------------------------------------------------------------- #
# Prepare (derive amount due from the GL — no posting)                         #
# --------------------------------------------------------------------------- #

def prepare_filing(obligation, *, period_start, period_end, due_date=None,
                   currency=None, actor_user=None):  # Public wrapper for draft filing preparation.
    """Create (or refresh) a DRAFT :class:`TaxFiling` with the amount owed read from the GL.

    Re-running for the same obligation/period updates the existing draft rather than
    duplicating it. Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:  # Atomic worker creates or refreshes the draft.
        return _prepare_filing_atomic(  # Prepare filing worksheet.
            obligation, period_start=period_start, period_end=period_end,  # Filing period bounds.
            due_date=due_date, currency=currency, actor_user=actor_user,  # Optional due date/currency and actor.
        )
    except FinanceError as exc:  # Failed preparation should be auditable.
        record_rejection(  # Record durable rejection.
            entity=obligation.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,  # Rejection audit action.
            exc=exc, actor_user=actor_user, target=obligation,  # Error, actor, and obligation context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _prepare_filing_atomic(obligation, *, period_start, period_end, due_date,
                           currency, actor_user):  # Transactional filing preparation.
    from .models import TaxFiling  # Local import avoids model import cycles.

    if not obligation.is_active:  # Inactive obligations cannot be filed.
        raise TaxFilingError(f"Tax obligation '{obligation.code}' is inactive.")
    if period_end < period_start:  # Filing period must be chronological.
        raise TaxFilingError("The filing period end cannot precede its start.")

    entity = obligation.entity  # Filing entity.
    debit, credit = _account_movement(  # Measure liability account movement in filing period.
        entity, obligation.liability_account,  # Entity and liability account.
        period_start=period_start, period_end=period_end,  # Filing period bounds.
    )
    gross = max(credit - debit, 0)  # credit-normal payable accrued in the period

    recoverable = 0  # Recoverable input tax that can offset liability.
    if obligation.recoverable_account_id:  # VAT-style obligations may have recoverable account.
        rdebit, rcredit = _account_movement(  # Measure recoverable account movement.
            entity, obligation.recoverable_account,  # Entity and recoverable account.
            period_start=period_start, period_end=period_end,  # Filing period bounds.
        )
        # Input tax is a debit-normal asset; never net below a zero remittance.
        recoverable = min(max(rdebit - rcredit, 0), gross)  # Cap offset at gross liability.

    filing = TaxFiling.objects.filter(  # Find exact draft to refresh.
        entity=entity, obligation=obligation,  # Same entity and obligation.
        period_start=period_start, period_end=period_end,  # Same filing period.
        filing_status=TaxFilingStatus.DRAFT,  # Only draft filings are refreshable.
    ).first()
    if filing is None:  # No exact draft exists; create after overlap check.
        # A new draft must not straddle any other filing for the same obligation.
        # Overlap: existing.period_start <= new.period_end AND existing.period_end >= new.period_start.
        # The exact-match draft above is the refresh path and is excluded here.  # Prevent duplicate/overlapping returns.
        clash = (  # Check for overlapping filing periods.
            TaxFiling.objects.filter(  # Same entity/obligation filings that overlap.
                entity=entity, obligation=obligation,  # Filing scope.
                period_start__lte=period_end, period_end__gte=period_start,  # Date overlap condition.
            )
            .exclude(  # Exclude the exact draft refresh case.
                period_start=period_start, period_end=period_end,  # Same bounds.
                filing_status=TaxFilingStatus.DRAFT,  # Same draft status.
            )
            .order_by("period_start")  # Stable earliest clash.
            .first()  # Return one clash or None.
        )
        if clash is not None:  # Overlap is not allowed.
            raise TaxFilingError(
                f"Filing period {period_start}–{period_end} overlaps existing "
                f"{obligation.code} filing {clash.document_number or clash.pk} "
                f"({clash.period_start}–{clash.period_end}).",
            )
        filing = TaxFiling(  # Initialize new draft filing.
            entity=entity, obligation=obligation,  # Scope and obligation.
            period_start=period_start, period_end=period_end,  # Filing period.
        )
    # A caller-supplied due_date always wins; when None, default it deterministically to
    # the obligation's filing_day of the month after period_end (overwriting a stale one
    # on refresh, so the default stays consistent with the obligation).  # Keeps refresh idempotent.
    filing.due_date = (  # Set filing due date.
        due_date if due_date is not None  # Explicit due date wins.
        else _default_due_date(period_end, obligation.filing_day)  # Otherwise deterministic default.
    )
    filing.currency = currency or filing.currency  # Preserve existing currency unless caller supplies one.
    filing.gross_liability = gross  # Store liability accrued in filing period.
    filing.recoverable_amount = recoverable  # Store recoverable offset.
    filing.adjustment_amount = int(filing.adjustment_amount or 0)  # Normalize existing adjustment.
    filing.recompute_due(save=False)  # Compute amount_due from gross/recoverable/adjustment.
    filing.save()  # Persist new or refreshed draft.

    record(  # Audit successful preparation.
        entity=entity, action=FinanceAuditAction.TAX_FILING_PREPARED,  # Audit action.
        actor_user=actor_user, target=filing,  # Actor and target context.
        message=(  # Human-readable preparation summary.
            f"Prepared {obligation.code} filing for {period_start}–{period_end}: "  # Obligation and period.
            f"{filing.amount_due} kobo due."  # Amount due.
        ),
        total=filing.amount_due, tax=recoverable,  # Structured metadata.
    )
    return filing  # Return draft filing.


# --------------------------------------------------------------------------- #
# File (freeze figures; net input VAT + book any penalty)                      #
# --------------------------------------------------------------------------- #

def file_filing(filing, *, filed_date, filing_reference="", adjustment_amount=0,
                adjustment_account=None, actor_user=None):  # Public wrapper for filing submission.
    """Submit a draft return: freeze figures, net recoverable input VAT, book any penalty.

    ``adjustment_amount`` (kobo) is a late-filing penalty / interest added to the amount
    due, posted ``Dr adjustment_account (expense), Cr liability``. Records a durable
    rejection audit on any :class:`FinanceError`.
    """
    try:  # Atomic worker freezes filing and posts adjustments.
        return _file_filing_atomic(  # Submit filing.
            filing, filed_date=filed_date, filing_reference=filing_reference,  # Submission date/reference.
            adjustment_amount=adjustment_amount, adjustment_account=adjustment_account,  # Optional penalty/interest.
            actor_user=actor_user,  # Acting user.
        )
    except FinanceError as exc:  # Failed filing should be auditable.
        record_rejection(  # Record durable rejection.
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,  # Rejection action.
            exc=exc, actor_user=actor_user, target=filing,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _file_filing_atomic(filing, *, filed_date, filing_reference, adjustment_amount,
                        adjustment_account, actor_user):  # Transactional filing submission.
    from .models import JournalEntry, JournalLine  # Journal models used for filing adjustment entry.

    if filing.filing_status != TaxFilingStatus.DRAFT:  # Only draft returns can be filed.
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is '{filing.filing_status}', "
            f"only a draft can be filed.",
        )

    adjustment_amount = int(adjustment_amount or 0)  # Normalize penalty/interest amount.
    if adjustment_amount < 0:  # Adjustments cannot reduce tax due here.
        raise TaxFilingError("A penalty/interest adjustment cannot be negative.")
    if adjustment_amount and adjustment_account is None:  # Expense account required when booking adjustment.
        raise TaxFilingError("A penalty/interest adjustment needs an expense account.")

    filing.adjustment_amount = adjustment_amount  # Store filing adjustment.
    filing.adjustment_account = adjustment_account  # Store adjustment expense account.
    filing.recompute_due(save=False)  # Recompute amount due after adjustment.
    if filing.amount_due <= 0:  # Nothing payable means no return to file in this workflow.
        raise TaxFilingError("Nothing to file — the computed amount due is zero.")

    obligation = filing.obligation  # Tax obligation being filed.
    recoverable = int(filing.recoverable_amount)  # Recoverable input amount to net.

    entry = None  # Filing may not need a journal when no netting/adjustment exists.
    if recoverable > 0 or adjustment_amount > 0:  # Journal required for VAT netting or penalties.
        period = resolve_period(filing.entity, filed_date)  # Resolve filing period.
        entry = JournalEntry.objects.create(  # Create filing adjustment journal.
            entity=filing.entity, branch=filing.branch, date=filed_date, period=period,  # Scope/date/period.
            source=JournalSource.CLOSING, currency=filing.currency,  # Closing-source adjustment.
            narration=f"Tax filing {filing.document_number or ''}: {obligation.code}".strip(),  # Narration.
            created_by=actor_user,  # Posting actor.
        )
        line_no = 0  # Journal line counter.
        if recoverable > 0:  # Net recoverable input tax against output liability.
            # Net recoverable input VAT off the output payable.  # Clears recoverable asset.
            line_no += 1  # First netting line.
            JournalLine.objects.create(  # Debit tax liability.
                entry=entry, account=obligation.liability_account,  # Output/payable account.
                debit=recoverable, credit=0,  # Dr liability.
                description="Net input VAT against output", line_no=line_no,  # Label and order.
            )
            line_no += 1  # Second netting line.
            JournalLine.objects.create(  # Credit recoverable input tax asset.
                entry=entry, account=obligation.recoverable_account,  # Recoverable account.
                debit=0, credit=recoverable,  # Cr recoverable asset.
                description="Clear recoverable input VAT", line_no=line_no,  # Label and order.
            )
        if adjustment_amount > 0:  # Penalty/interest increases amount due.
            # Penalty / interest increases the payable.  # Debit expense, credit tax payable.
            line_no += 1  # Penalty expense line.
            JournalLine.objects.create(  # Debit penalty/interest expense.
                entry=entry, account=adjustment_account,  # Expense account.
                debit=adjustment_amount, credit=0,  # Dr penalty expense.
                description="Tax penalty / interest", line_no=line_no,  # Label and order.
            )
            line_no += 1  # Payable increase line.
            JournalLine.objects.create(  # Credit tax liability.
                entry=entry, account=obligation.liability_account,  # Liability account.
                debit=0, credit=adjustment_amount,  # Cr tax payable.
                description="Penalty added to payable", line_no=line_no,  # Label and order.
            )
        post_journal(entry, actor_user=actor_user)  # Validate and post filing adjustment journal.
        filing.filing_journal = entry  # Link filing to adjustment journal.

    filing.filing_reference = filing_reference  # Store external filing reference.
    filing.filed_at = filed_date  # Store filing submission date.
    filing.filing_status = TaxFilingStatus.FILED  # Mark return filed.
    filing.save()  # Persist all filing fields.

    record(  # Audit successful filing.
        entity=filing.entity, action=FinanceAuditAction.TAX_FILING_FILED,  # Audit action.
        actor_user=actor_user, target=filing,  # Actor and target context.
        message=(  # Human-readable filing summary.
            f"Filed {obligation.code} return {filing.document_number or filing.pk} "  # Obligation and filing id.
            f"({filing.amount_due} kobo due)."  # Amount due.
        ),
        journal_id=entry.pk if entry else None,  # Adjustment journal id when one exists.
        total=filing.amount_due, tax=filing.recoverable_amount,  # Structured metadata.
    )
    return filing  # Return filed return.


# --------------------------------------------------------------------------- #
# Unfile (revert a FILED return to DRAFT — the audit-correct undo)             #
# --------------------------------------------------------------------------- #

def unfile_filing(filing, *, actor_user=None):  # Public wrapper for reverting a filed return.
    """Revert a FILED return to DRAFT: reverse its netting/penalty journal, clear filing.

    The undo for a return filed in error, before any remittance. A PAID return is
    refused (reverse the remittance first); so is a return with any payment recorded.
    Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:  # Atomic worker reverses filing state.
        return _unfile_filing_atomic(filing, actor_user=actor_user)  # Un-file return.
    except FinanceError as exc:  # Failed unfile should be auditable.
        record_rejection(  # Record durable rejection.
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,  # Rejection action.
            exc=exc, actor_user=actor_user, target=filing,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _unfile_filing_atomic(filing, *, actor_user=None):  # Transactional filing reversal.
    from .posting import reverse_journal  # Local import avoids circular service import.

    if filing.filing_status == TaxFilingStatus.PAID:  # Paid returns require remittance reversal first.
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is PAID; reverse the "
            f"remittance before un-filing it.",
        )
    if filing.filing_status != TaxFilingStatus.FILED:  # Only filed returns can be un-filed.
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is '{filing.filing_status}', "
            f"only a filed return can be un-filed.",
        )
    if int(filing.amount_paid or 0) > 0:  # Any remittance must be reversed before unfiling.
        raise TaxFilingError(
            "This filing carries a remittance; reverse the payment before un-filing it.",
        )

    reversed_journal_id = None  # Track journal reversed for audit.
    if filing.filing_journal_id is not None:  # Filing may have netting/penalty journal.
        reverse_journal(filing.filing_journal, actor_user=actor_user)  # Reverse filing adjustment journal.
        reversed_journal_id = filing.filing_journal_id  # Preserve id before unlinking.
        filing.filing_journal = None  # Clear filing journal link.

    filing.filed_at = None  # Clear submitted date.
    filing.filing_reference = ""  # Clear external filing reference.
    filing.filing_status = TaxFilingStatus.DRAFT  # Return to draft status.
    filing.save(update_fields=[  # Persist unfile fields.
        "filing_journal", "filed_at", "filing_reference", "filing_status", "updated_at",  # Fields changed.
    ])

    record(  # Audit successful unfile.
        entity=filing.entity, action=FinanceAuditAction.TAX_FILING_UNFILED,  # Audit action.
        actor_user=actor_user, target=filing,  # Actor and target context.
        message=(  # Human-readable summary.
            f"Un-filed {filing.obligation.code} return "  # Obligation code.
            f"{filing.document_number or filing.pk} back to draft."  # Filing id and state.
        ),
        journal_id=reversed_journal_id,  # Reversed filing journal id.
    )
    return filing  # Return draft filing.


# --------------------------------------------------------------------------- #
# Pay / remit (Dr liability, Cr bank)                                          #
# --------------------------------------------------------------------------- #

def pay_filing(filing, *, bank_account, pay_date, amount=None, actor_user=None):  # Public wrapper for tax remittance.
    """Remit a filed return: ``Dr liability, Cr bank``.

    ``amount`` defaults to the full outstanding balance; a smaller amount records a
    partial remittance. Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:  # Atomic worker posts remittance.
        return _pay_filing_atomic(  # Pay filing amount.
            filing, bank_account=bank_account, pay_date=pay_date,  # Payment bank and date.
            amount=amount, actor_user=actor_user,  # Optional partial amount and actor.
        )
    except FinanceError as exc:  # Failed payment should be auditable.
        record_rejection(  # Record durable rejection.
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,  # Rejection action.
            exc=exc, actor_user=actor_user, target=filing,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _pay_filing_atomic(filing, *, bank_account, pay_date, amount, actor_user):  # Transactional remittance posting.
    from .models import JournalEntry, JournalLine  # Journal models used for payment entry.

    if filing.filing_status not in (TaxFilingStatus.FILED, TaxFilingStatus.PAID):  # Only filed returns can receive payments.
        raise TaxFilingError("Only a filed return can be remitted.")
    if bank_account.entity_id != filing.entity_id:  # Bank account must belong to filing entity.
        raise TaxFilingError("The bank account belongs to a different entity.")
    outstanding = filing.balance_due  # Amount still unpaid.
    if outstanding <= 0:  # Nothing left to remit.
        raise TaxFilingError("This filing has no outstanding balance to remit.")
    pay = outstanding if amount is None else min(int(amount), outstanding)  # Default to full balance and cap partials.
    if pay <= 0:  # Remittance amount must be positive.
        raise TaxFilingError("Remittance amount must be positive.")

    obligation = filing.obligation  # Obligation being remitted.
    period = resolve_period(filing.entity, pay_date)  # Resolve remittance period.
    entry = JournalEntry.objects.create(  # Create remittance journal header.
        entity=filing.entity, branch=filing.branch, date=pay_date, period=period,  # Scope/date/period.
        source=JournalSource.BANK, currency=filing.currency,  # Bank-source cash payment.
        narration=f"Remit {obligation.code} {filing.document_number or ''}".strip(),  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(  # Debit tax liability.
        entry=entry, account=obligation.liability_account, debit=pay, credit=0,  # Dr payable.
        description=f"{obligation.code} remitted to {obligation.authority_name or 'authority'}",  # Authority label.
        line_no=1,  # First line.
    )
    JournalLine.objects.create(  # Credit bank account.
        entry=entry, account=bank_account.gl_account, debit=0, credit=pay,  # Cr bank.
        description="Tax remittance paid", line_no=2,  # Label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post remittance journal.

    filing.amount_paid = int(filing.amount_paid) + pay  # Accumulate remitted amount.
    filing.refresh_payment_status(save=False)  # Recompute unpaid/partial/paid status.
    if filing.payment_status == InvoicePaymentStatus.PAID:  # Fully remitted return.
        filing.filing_status = TaxFilingStatus.PAID  # Mark lifecycle paid.
    filing.save(update_fields=[  # Persist payment fields.
        "amount_paid", "payment_status", "filing_status", "updated_at",  # Fields changed by remittance.
    ])

    record(  # Audit successful remittance.
        entity=filing.entity, action=FinanceAuditAction.TAX_FILING_PAID,  # Audit action.
        actor_user=actor_user, target=filing,  # Actor and target context.
        message=(  # Human-readable payment summary.
            f"Remitted {pay} kobo of {obligation.code} filing "  # Amount and obligation.
            f"{filing.document_number or filing.pk}."  # Filing identifier.
        ),
        journal_id=entry.pk, amount=pay, payment_status=filing.payment_status,  # Structured metadata.
    )
    return filing  # Return updated filing.


# --------------------------------------------------------------------------- #
# Read-only — what each obligation currently owes                              #
# --------------------------------------------------------------------------- #

def outstanding_obligations(entity) -> list:  # Snapshot unremitted tax balances by obligation.
    """Per-obligation snapshot of the unremitted balance sitting in each control account.

    The running net credit balance of each active obligation's ``liability_account`` (less
    any recoverable input balance), i.e. what would be owed if a return were filed for all
    activity to date. Returns one dict per active obligation.
    """
    from .models import TaxObligation  # Local import avoids model import cycles.

    rows = []  # Result rows.
    qs = (  # Active obligations for this entity.
        TaxObligation.objects  # Start from obligation manager.
        .filter(entity=entity, is_active=True)  # Active obligations in entity.
        .select_related("liability_account", "recoverable_account")  # Load control accounts.
        .order_by("code")  # Stable display order.
    )
    for ob in qs:  # Build one snapshot per obligation.
        debit, credit = _account_movement(entity, ob.liability_account)  # Liability account all-time movement.
        payable = credit - debit  # Credit-normal payable balance.
        recoverable = 0  # Recoverable offset defaults to none.
        if ob.recoverable_account_id:  # VAT-style obligations may have recoverable account.
            rdebit, rcredit = _account_movement(entity, ob.recoverable_account)  # Recoverable account movement.
            recoverable = rdebit - rcredit  # Debit-normal recoverable balance.
        net = max(payable - max(recoverable, 0), 0)  # Net amount owed after recoverable offset.
        rows.append({  # Append obligation snapshot.
            "obligation_id": ob.id, "code": ob.code, "name": ob.name,  # Obligation identity.
            "obligation_type": ob.obligation_type,  # VAT/WHT/PAYE/etc.
            "authority_name": ob.authority_name,  # Filing authority.
            "liability_code": ob.liability_account.code,  # Liability account code.
            "payable_balance": payable,  # Gross payable balance.
            "recoverable_balance": recoverable,  # Recoverable balance.
            "net_outstanding": net,  # Net amount currently owed.
        })
    return rows  # Return obligation snapshots.
