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
    BankMatchSource,
    BankReconStatus,
    BankStatementStatus,
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    NormalBalance,
)
from .exceptions import BankReconciliationError
from .posting import post_journal, resolve_period, reverse_journal


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
                           opening_balance=0, closing_balance=None, force=False, actor_user=None):
    """Import ``rows`` into a new :class:`BankStatement`. Returns
    ``(statement, created_lines, suspected_duplicates)``.

    ``rows`` is an iterable of dicts: ``{txn_date, amount, description?, reference?,
    external_id?}`` where ``amount`` is signed kobo. The batch is grouped under a
    :class:`BankStatement` (period opening → closing); when ``closing_balance`` is not
    given it is derived as ``opening + Σ amounts``.

    De-dup guards against an accidental re-upload silently doubling a bank charge:

    * a row whose ``external_id`` already exists for this account is skipped (exact dup);
    * a row **without** an ``external_id`` that matches an existing line on
      ``(txn_date, amount, description, reference)`` is treated as a *suspected*
      re-import — held back and returned in ``suspected_duplicates`` rather than
      imported — **unless** ``force`` is set.

    Two genuinely identical same-day transactions in one *fresh* batch are both kept
    (the check is against already-stored lines, not within the batch).
    """
    from .models import BankStatement, BankStatementLine

    rows = list(rows)
    created = []
    suspected = []
    movement = 0
    for row in rows:
        external_id = (row.get("external_id") or "").strip()
        if external_id and BankStatementLine.objects.filter(
            bank_account=bank_account, external_id=external_id,
        ).exists():
            continue
        amount = int(row["amount"])
        description = row.get("description", "")
        reference = row.get("reference", "")
        if (not force and not external_id and BankStatementLine.objects.filter(
            bank_account=bank_account, txn_date=row["txn_date"], amount=amount,
            description=description, reference=reference,
        ).exists()):
            suspected.append({
                "txn_date": row["txn_date"], "amount": amount,
                "description": description, "reference": reference,
            })
            continue
        created.append(BankStatementLine(
            bank_account=bank_account,
            txn_date=row["txn_date"],
            amount=amount,
            description=description,
            reference=reference,
            external_id=external_id,
        ))
        movement += amount

    if not created:
        return None, [], suspected

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
    return statement, list(BankStatementLine.objects.filter(statement=statement)), suspected


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #

#: Largest "one bank line covers N receipts" group auto-match tries.
GROUP_AUTO_MATCH_MAX = 4
#: Skip group auto-match when a statement line has more candidate GL lines than this
#: (keeps the bounded subset search cheap and the result unambiguous).
_GROUP_AUTO_MATCH_POOL_CAP = 12


def _signed_gl(line) -> int:
    """A cash-account journal line's signed contribution in kobo (debit - credit)."""
    return (line.debit or 0) - (line.credit or 0)


def _unique_summing_subset(lines, target, *, max_size):
    """The **unique** subset (size 2..``max_size``) of ``lines`` whose signed amounts
    sum to ``target`` — or ``None`` when there is no such subset **or more than one**
    (ambiguous). Conservative on purpose: auto-grouping only fires when there's a single
    unambiguous answer.
    """
    from itertools import combinations

    found = None
    for size in range(2, min(max_size, len(lines)) + 1):
        for combo in combinations(lines, size):
            if sum(_signed_gl(gl) for gl in combo) == target:
                if found is not None:
                    return None  # more than one subset fits → ambiguous
                found = combo
    return found


def _unmatched_gl_lines(bank_account):
    """Posted cash-account journal lines not yet paired to a statement line."""
    from .models import JournalLine

    return (
        JournalLine.objects
        .filter(
            account=bank_account.gl_account,
            entry__status=DocumentStatus.POSTED,
        )
        # Not paired either 1:1 (matched_line) or as part of a group match.
        .filter(bank_statement_lines__isnull=True, bank_line_matches__isnull=True)
        .select_related("entry")
        .order_by("entry__date", "id")
    )


@transaction.atomic
def auto_reconcile(bank_account, *, tolerance_days=4, group=True,
                   max_group=GROUP_AUTO_MATCH_MAX, actor_user=None):
    """Pair unmatched statement lines to unmatched GL cash lines by amount + date.

    **First pass — 1:1:** a statement line auto-matches a posted cash-account journal
    line with the **same signed amount** whose journal date is within ``tolerance_days``,
    but only when there is **exactly one** such unconsumed candidate; ambiguous ties are
    left for a human.

    **Second pass — group (when ``group``):** for each still-unmatched line, if a
    **unique** small subset (size 2..``max_group``) of same-direction, in-tolerance GL
    lines *sums* to it (one bank line covering several receipts), they are group-matched
    via :class:`~vs_finance.models.BankLineMatch`. Skipped when the candidate pool is
    large (ambiguity/cost). Each GL line is consumed at most once. Returns the statement
    lines newly matched.
    """
    from .models import BankLineMatch, BankStatementLine

    pending = list(
        BankStatementLine.objects
        .filter(bank_account=bank_account, status=BankLineStatus.UNMATCHED)
        .order_by("txn_date", "id")
    )
    gl_lines = list(_unmatched_gl_lines(bank_account))
    consumed: set[int] = set()
    matched = []

    def _mark(sline):
        sline.status = BankLineStatus.MATCHED
        sline.match_source = BankMatchSource.AUTO
        sline.reconciled_at = timezone.now()

    for sline in pending:
        candidates = [
            gl for gl in gl_lines
            if gl.id not in consumed
            and _signed_gl(gl) == sline.amount
            and abs((gl.entry.date - sline.txn_date).days) <= tolerance_days
        ]
        if len(candidates) != 1:
            continue  # 0 = no match; >1 = ambiguous, leave it for a human
        gl = candidates[0]
        sline.matched_line = gl
        _mark(sline)
        sline.save(update_fields=["matched_line", "status", "match_source", "reconciled_at", "updated_at"])
        consumed.add(gl.id)
        matched.append(sline)

    if group:
        # Second pass: group several GL lines that SUM to a still-unmatched bank line.
        for sline in pending:
            if sline.status != BankLineStatus.UNMATCHED:
                continue
            pool = [
                gl for gl in gl_lines
                if gl.id not in consumed
                and (_signed_gl(gl) > 0) == (sline.amount > 0)  # same direction
                and abs((gl.entry.date - sline.txn_date).days) <= tolerance_days
            ]
            if not (2 <= len(pool) <= _GROUP_AUTO_MATCH_POOL_CAP):
                continue
            subset = _unique_summing_subset(pool, sline.amount, max_size=max_group)
            if subset is None:
                continue
            BankLineMatch.objects.bulk_create(
                [BankLineMatch(statement_line=sline, journal_line=gl) for gl in subset],
            )
            _mark(sline)
            sline.save(update_fields=["status", "match_source", "reconciled_at", "updated_at"])
            consumed.update(gl.id for gl in subset)
            matched.append(sline)

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
    statement_line.match_source = BankMatchSource.MANUAL
    statement_line.reconciled_at = timezone.now()
    statement_line.save(
        update_fields=["matched_line", "status", "match_source", "reconciled_at", "updated_at"],
    )
    return statement_line


@transaction.atomic
def group_match(statement_line, journal_lines, *, actor_user=None):
    """Match one statement line to **several** cash journal lines that sum to its amount.

    The many-to-one case: one bank line (e.g. a PSP settlement) settling multiple
    ledger movements. Each line must be posted, on this bank's GL cash account, and not
    already matched (1:1 or in another group); their signed amounts
    (``debit − credit``) must total the statement line's signed amount exactly. Records
    a :class:`~vs_finance.models.BankLineMatch` per pair — no ledger effect. Returns the
    statement line.
    """
    from .models import BankLineMatch

    if statement_line.status != BankLineStatus.UNMATCHED:
        raise BankReconciliationError(
            f"Statement line is '{statement_line.status}', only an unmatched line can be matched.",
        )
    lines = list(journal_lines)
    if len(lines) < 2:
        raise BankReconciliationError(
            "A group match needs at least two journal lines (use match for a single line).",
        )

    bank_account = statement_line.bank_account
    seen: set[int] = set()
    total = 0
    for jl in lines:
        if jl.id in seen:
            raise BankReconciliationError("A journal line appears twice in the group.")
        seen.add(jl.id)
        if jl.account_id != bank_account.gl_account_id:
            raise BankReconciliationError(
                "A journal line is not on this bank account's GL cash account.",
            )
        if jl.entry.status != DocumentStatus.POSTED:
            raise BankReconciliationError("Only posted journal lines can be matched.")
        if jl.bank_statement_lines.exists() or jl.bank_line_matches.exists():
            raise BankReconciliationError(f"Journal line {jl.id} is already matched.")
        total += _signed_gl(jl)

    if total != statement_line.amount:
        raise BankReconciliationError(
            f"Group total {total} kobo does not equal the statement line "
            f"{statement_line.amount} kobo.",
        )

    BankLineMatch.objects.bulk_create(
        [BankLineMatch(statement_line=statement_line, journal_line=jl) for jl in lines],
    )
    statement_line.status = BankLineStatus.MATCHED
    statement_line.match_source = BankMatchSource.MANUAL
    statement_line.reconciled_at = timezone.now()
    statement_line.save(
        update_fields=["status", "match_source", "reconciled_at", "updated_at"],
    )
    record(
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,
        actor_user=actor_user, target=bank_account,
        message=f"Group-matched a statement line to {len(lines)} journal line(s) "
                f"on {bank_account.name}.",
        bank_account_id=bank_account.id, journal_lines=len(lines),
    )
    return statement_line


@transaction.atomic
def split_match(journal_line, statement_lines, *, actor_user=None):
    """Match **one** cash journal line to **several** statement lines that sum to it.

    The reverse of :func:`group_match`: one ledger movement the bank reported as several
    lines (e.g. a payout split into principal + fee). Each statement line must be
    unmatched and on the same bank account as the journal line's GL cash account; their
    signed amounts total the journal line's signed amount. Records a
    :class:`~vs_finance.models.BankLineMatch` per statement line — no ledger effect.
    Unmatching any one of them later frees just that line (see :func:`unmatch_line`).
    """
    from .models import BankLineMatch

    slines = list(statement_lines)
    if len(slines) < 2:
        raise BankReconciliationError(
            "A split match needs at least two statement lines (use match for one).",
        )
    bank_account = slines[0].bank_account
    if journal_line.account_id != bank_account.gl_account_id:
        raise BankReconciliationError(
            "The journal line is not on this bank account's GL cash account.",
        )
    if journal_line.entry.status != DocumentStatus.POSTED:
        raise BankReconciliationError("Only a posted journal line can be matched.")
    if journal_line.bank_statement_lines.exists() or journal_line.bank_line_matches.exists():
        raise BankReconciliationError(f"Journal line {journal_line.id} is already matched.")

    seen: set[int] = set()
    total = 0
    for sl in slines:
        if sl.id in seen:
            raise BankReconciliationError("A statement line appears twice in the split.")
        seen.add(sl.id)
        if sl.bank_account_id != bank_account.id:
            raise BankReconciliationError("All statement lines must belong to the same bank account.")
        if sl.status != BankLineStatus.UNMATCHED:
            raise BankReconciliationError(
                f"Statement line {sl.id} is '{sl.status}', only an unmatched line can be matched.",
            )
        total += sl.amount

    if total != _signed_gl(journal_line):
        raise BankReconciliationError(
            f"Statement lines sum to {total} kobo, not the journal line's "
            f"{_signed_gl(journal_line)} kobo.",
        )

    BankLineMatch.objects.bulk_create(
        [BankLineMatch(statement_line=sl, journal_line=journal_line) for sl in slines],
    )
    now = timezone.now()
    for sl in slines:
        sl.status = BankLineStatus.MATCHED
        sl.match_source = BankMatchSource.MANUAL
        sl.reconciled_at = now
        sl.save(update_fields=["status", "match_source", "reconciled_at", "updated_at"])
    record(
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,
        actor_user=actor_user, target=bank_account,
        message=f"Split-matched journal line {journal_line.id} across {len(slines)} "
                f"statement line(s) on {bank_account.name}.",
        bank_account_id=bank_account.id, statement_lines=len(slines),
    )
    return slines


@transaction.atomic
def unmatch_line(statement_line, *, actor_user=None):
    """Undo a match — and reverse the adjusting journal if the match created one.

    A plain match just drops the pairing (no ledger effect). A match that booked
    an adjusting journal reverses that journal (a mirror entry that nets to zero),
    so unmatching never silently leaves the ledger out of step.
    """
    if statement_line.status != BankLineStatus.MATCHED:
        raise BankReconciliationError("Only a matched line can be unmatched.")
    bank_account = statement_line.bank_account
    adj = statement_line.adjusting_journal
    if adj is not None:
        reverse_journal(adj, actor_user=actor_user)
    # Drop any group-match links (many-to-one); these carry no ledger effect.
    statement_line.line_matches.all().delete()
    statement_line.matched_line = None
    statement_line.adjusting_journal = None
    statement_line.status = BankLineStatus.UNMATCHED
    statement_line.match_source = ""
    statement_line.reconciled_at = None
    statement_line.save(update_fields=[
        "matched_line", "adjusting_journal", "status", "match_source",
        "reconciled_at", "updated_at",
    ])
    record(
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,
        actor_user=actor_user, target=bank_account,
        message=f"Unmatched a statement line on {bank_account.name}"
                + (f" and reversed adjusting journal {adj.pk}." if adj else "."),
        bank_account_id=bank_account.id,
    )
    return statement_line


@transaction.atomic
def set_line_ignored(statement_line, *, ignored=True, reason="", actor_user=None):
    """Mark an unmatched statement line ``IGNORED`` (a known duplicate / opening-balance
    line), or revert an ignored line back to ``UNMATCHED``.

    Ignored lines carry no ledger effect and drop out of the unreconciled count (which
    only counts ``UNMATCHED``), so a statement of MATCHED + IGNORED lines can still
    reconcile. Only unmatched↔ignored transitions are allowed.
    """
    if ignored:
        if statement_line.status != BankLineStatus.UNMATCHED:
            raise BankReconciliationError(
                f"Statement line is '{statement_line.status}', only an unmatched line can be ignored.",
            )
        statement_line.status = BankLineStatus.IGNORED
    else:
        if statement_line.status != BankLineStatus.IGNORED:
            raise BankReconciliationError("Only an ignored line can be un-ignored.")
        statement_line.status = BankLineStatus.UNMATCHED
    statement_line.save(update_fields=["status", "updated_at"])
    record(
        entity=statement_line.bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,
        actor_user=actor_user, target=statement_line.bank_account,
        message=(f"{'Ignored' if ignored else 'Un-ignored'} a statement line on "
                 f"{statement_line.bank_account.name}." + (f" Reason: {reason}" if reason else "")),
        bank_account_id=statement_line.bank_account_id,
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
    statement_line.match_source = BankMatchSource.ADJUSTMENT
    statement_line.reconciled_at = timezone.now()
    statement_line.save(update_fields=[
        "adjusting_journal", "matched_line", "status", "match_source",
        "reconciled_at", "updated_at",
    ])

    record(
        entity=entity, action=FinanceAuditAction.BANK_CHARGE_POSTED,
        actor_user=actor_user, target=bank_account,
        message=f"Booked bank adjustment {magnitude} kobo on {bank_account.name}.",
        journal_id=entry.pk, amount=statement_line.amount,
    )
    return entry
