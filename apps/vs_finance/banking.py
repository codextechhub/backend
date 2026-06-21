"""Banking services — statement import and bank reconciliation.

The bank balance the ledger believes (the GL cash account) and the balance the bank
reports (the statement) drift apart for honest reasons: in-flight cheques, charges the
bank deducted that the books don't know about yet, interest credited. Reconciliation
is the discipline of explaining every difference — pairing each statement line to a
ledger movement, and raising an *adjusting journal* for anything the books are missing.

A bank-statement ``amount`` is **signed from our perspective**: positive is money in
(a GL **debit** to the cash account), negative is money out (a GL **credit**). A GL
cash-account line's signed contribution is therefore ``debit - credit``, and a line
matches a statement line when those equal. All amounts are integer kobo.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .accounts import resolve_account
from .audit import record
from .constants import (
    BankLineStatus,
    BankReconStatus,
    BankStatementStatus,
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    NormalBalance,
)
from .exceptions import BankReconciliationError
from .posting import post_journal, resolve_period


# --------------------------------------------------------------------------- #
# Statement import                                                            #
# --------------------------------------------------------------------------- #

def gl_account_balance(account) -> int:
    """Net posted balance of a GL ``account`` in kobo, signed to its normal side."""
    from django.db.models import Sum
    from .models import AccountBalance

    agg = AccountBalance.objects.filter(account=account).aggregate(
        d=Sum("debit_total"), c=Sum("credit_total"))
    net = (agg["d"] or 0) - (agg["c"] or 0)
    if account.normal_balance != NormalBalance.DEBIT:
        net = -net
    return int(net)


@transaction.atomic
def import_statement_lines(bank_account, rows, *, statement_date=None, period_label="",
                           opening_balance=0, closing_balance=None, actor_user=None):
    """Import ``rows`` into a new :class:`BankStatement` (idempotent on ``external_id``).

    ``rows`` is an iterable of dicts: ``{txn_date, amount, description?, reference?,
    external_id?}`` where ``amount`` is signed kobo. The batch is grouped under a
    :class:`BankStatement` (period opening → closing); when ``closing_balance`` is not
    given it is derived as ``opening + Σ amounts``. Lines whose ``external_id`` already
    exists for this account are skipped. Returns ``(statement, created_lines)``.
    """
    from .models import BankStatement, BankStatementLine

    rows = list(rows)
    created = []
    movement = 0
    for row in rows:
        external_id = (row.get("external_id") or "").strip()
        if external_id and BankStatementLine.objects.filter(
            bank_account=bank_account, external_id=external_id,
        ).exists():
            continue
        amount = int(row["amount"])
        created.append(BankStatementLine(
            bank_account=bank_account,
            txn_date=row["txn_date"],
            amount=amount,
            description=row.get("description", ""),
            reference=row.get("reference", ""),
            external_id=external_id,
        ))
        movement += amount

    if not created:
        return None, []

    opening_balance = int(opening_balance or 0)
    if closing_balance is None:
        closing_balance = opening_balance + movement
    statement = BankStatement.objects.create(
        bank_account=bank_account,
        statement_date=statement_date or max(l.txn_date for l in created),
        period_label=period_label or "",
        opening_balance=opening_balance, closing_balance=int(closing_balance),
        status=BankStatementStatus.UPLOADED, imported_by=actor_user,
    )
    for line in created:
        line.statement = statement
    BankStatementLine.objects.bulk_create(created)
    return statement, list(BankStatementLine.objects.filter(statement=statement))


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #

def _signed_gl(line) -> int:
    """A cash-account journal line's signed contribution in kobo (debit - credit)."""
    return (line.debit or 0) - (line.credit or 0)


def _unmatched_gl_lines(bank_account):
    """Posted cash-account journal lines not yet paired to a statement line."""
    from .models import JournalLine

    return (
        JournalLine.objects
        .filter(
            account=bank_account.gl_account,
            entry__status=DocumentStatus.POSTED,
        )
        .filter(bank_statement_lines__isnull=True)
        .select_related("entry")
        .order_by("entry__date", "id")
    )


@transaction.atomic
def auto_reconcile(bank_account, *, tolerance_days=4, actor_user=None):
    """Pair unmatched statement lines to unmatched GL cash lines by amount + date.

    A statement line matches the first available posted cash-account journal line with
    the **same signed amount** whose journal date is within ``tolerance_days`` of the
    statement date. Greedy and conservative: each GL line is consumed at most once, and
    anything ambiguous is simply left unmatched for a human to pair. Returns the list of
    statement lines newly matched.
    """
    from .models import BankStatementLine

    pending = list(
        BankStatementLine.objects
        .filter(bank_account=bank_account, status=BankLineStatus.UNMATCHED)
        .order_by("txn_date", "id")
    )
    gl_lines = list(_unmatched_gl_lines(bank_account))
    consumed: set[int] = set()
    matched = []

    for sline in pending:
        for gl in gl_lines:
            if gl.id in consumed:
                continue
            if _signed_gl(gl) != sline.amount:
                continue
            if abs((gl.entry.date - sline.txn_date).days) > tolerance_days:
                continue
            sline.matched_line = gl
            sline.status = BankLineStatus.MATCHED
            sline.reconciled_at = timezone.now()
            sline.save(update_fields=["matched_line", "status", "reconciled_at", "updated_at"])
            consumed.add(gl.id)
            matched.append(sline)
            break

    if matched:
        record(
            entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,
            actor_user=actor_user, target=bank_account,
            message=f"Auto-matched {len(matched)} line(s) on {bank_account.name}.",
            matched=len(matched), bank_account_id=bank_account.id,
        )
        _record_reconciliation(bank_account, matched_count=len(matched), actor_user=actor_user)
    return matched


def statement_balance(bank_account) -> int | None:
    """The most recent imported statement's closing balance (kobo), or None."""
    latest = bank_account.statements.order_by("-statement_date", "-id").first()
    return int(latest.closing_balance) if latest else None


def _record_reconciliation(bank_account, *, matched_count, actor_user=None):
    """Snapshot book vs statement balance after a reconcile, and close clean statements."""
    from .models import BankReconciliation

    book = gl_account_balance(bank_account.gl_account)
    stmt = statement_balance(bank_account)
    stmt_val = stmt if stmt is not None else book
    difference = book - stmt_val
    recon = BankReconciliation.objects.create(
        bank_account=bank_account, as_of_date=timezone.now().date(),
        book_balance=book, statement_balance=stmt_val, difference=difference,
        matched_count=matched_count,
        status=BankReconStatus.BALANCED if difference == 0 else BankReconStatus.OUT_OF_BALANCE,
        performed_by=actor_user,
        statement=bank_account.statements.order_by("-statement_date", "-id").first(),
    )
    # A statement with no remaining unmatched lines is fully reconciled.
    for st in bank_account.statements.filter(status=BankStatementStatus.UPLOADED):
        if not st.lines.filter(status=BankLineStatus.UNMATCHED).exists() and st.lines.exists():
            st.status = BankStatementStatus.RECONCILED
            st.save(update_fields=["status", "updated_at"])
    return recon


@transaction.atomic
def complete_reconciliation(bank_account, *, actor_user=None):
    """Finalise a reconciliation — record a snapshot of the account's current state."""
    from .models import BankStatementLine

    matched = BankStatementLine.objects.filter(
        bank_account=bank_account, status=BankLineStatus.MATCHED).count()
    recon = _record_reconciliation(bank_account, matched_count=matched, actor_user=actor_user)
    record(
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,
        actor_user=actor_user, target=bank_account,
        message=f"Reconciliation completed on {bank_account.name} "
                f"(diff {recon.difference} kobo).",
        bank_account_id=bank_account.id, difference=recon.difference,
    )
    return recon


@transaction.atomic
def match_line(statement_line, journal_line, *, actor_user=None):
    """Manually pair a statement line to a specific cash-account journal line."""
    bank_account = statement_line.bank_account
    if journal_line.account_id != bank_account.gl_account_id:
        raise BankReconciliationError(
            "The journal line is not on this bank account's GL cash account.",
        )
    if _signed_gl(journal_line) != statement_line.amount:
        raise BankReconciliationError(
            f"Amount mismatch: statement {statement_line.amount} kobo vs journal line "
            f"{_signed_gl(journal_line)} kobo.",
        )
    statement_line.matched_line = journal_line
    statement_line.status = BankLineStatus.MATCHED
    statement_line.reconciled_at = timezone.now()
    statement_line.save(
        update_fields=["matched_line", "status", "reconciled_at", "updated_at"],
    )
    return statement_line


# --------------------------------------------------------------------------- #
# Adjusting journals (book what the statement reveals)                        #
# --------------------------------------------------------------------------- #

@transaction.atomic
def post_bank_adjustment(statement_line, *, counter_account=None, counter_code=None,
                         narration="", actor_user=None):
    """Book an unrecorded statement line (charge/interest) and match it.

    For a line the books don't yet know about — a bank charge (outflow) or interest
    (inflow) — raise the adjusting journal against ``counter_account`` (or resolve
    ``counter_code``; defaults to ``5500 Bank Charges``) and the bank's cash account,
    then reconcile the statement line to the new cash line. Direction follows the sign
    of ``amount``:

    * outflow (amount < 0): ``Dr counter (expense), Cr cash``
    * inflow  (amount > 0): ``Dr cash, Cr counter (income/contra)``
    """
    from .constants import BANK_CHARGES_CODE
    from .models import JournalEntry, JournalLine

    bank_account = statement_line.bank_account
    entity = bank_account.entity
    if statement_line.status != BankLineStatus.UNMATCHED:
        raise BankReconciliationError(
            f"Statement line is '{statement_line.status}', only an unmatched line can be adjusted.",
        )
    if statement_line.amount == 0:
        raise BankReconciliationError("Cannot adjust a zero-amount statement line.")

    if counter_account is None:
        counter_account = resolve_account(
            entity, counter_code or BANK_CHARGES_CODE, label="bank charge counter",
        )

    period = resolve_period(entity, statement_line.txn_date)
    cash = bank_account.gl_account
    magnitude = abs(statement_line.amount)
    inflow = statement_line.amount > 0

    entry = JournalEntry.objects.create(
        entity=entity, branch=bank_account.branch,
        date=statement_line.txn_date, period=period,
        source=JournalSource.BANK,
        narration=narration or statement_line.description or "Bank adjustment",
        reference=statement_line.reference, created_by=actor_user,
    )
    if inflow:
        cash_line = JournalLine.objects.create(
            entry=entry, account=cash, debit=magnitude, credit=0,
            description="Bank credit", line_no=1,
        )
        JournalLine.objects.create(
            entry=entry, account=counter_account, debit=0, credit=magnitude,
            description=statement_line.description or "Bank credit", line_no=2,
        )
    else:
        JournalLine.objects.create(
            entry=entry, account=counter_account, debit=magnitude, credit=0,
            description=statement_line.description or "Bank charge", line_no=1,
        )
        cash_line = JournalLine.objects.create(
            entry=entry, account=cash, debit=0, credit=magnitude,
            description="Bank charge", line_no=2,
        )

    post_journal(entry, actor_user=actor_user)

    statement_line.adjusting_journal = entry
    statement_line.matched_line = cash_line
    statement_line.status = BankLineStatus.MATCHED
    statement_line.reconciled_at = timezone.now()
    statement_line.save(update_fields=[
        "adjusting_journal", "matched_line", "status", "reconciled_at", "updated_at",
    ])

    record(
        entity=entity, action=FinanceAuditAction.BANK_CHARGE_POSTED,
        actor_user=actor_user, target=bank_account,
        message=f"Booked bank adjustment {magnitude} kobo on {bank_account.name}.",
        journal_id=entry.pk, amount=statement_line.amount,
    )
    return entry
