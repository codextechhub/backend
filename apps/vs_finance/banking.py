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
from __future__ import annotations  # Defer annotation evaluation for forward references.

from django.db import transaction  # Wrap bank matching and adjustment writes atomically.
from django.utils import timezone  # Timestamp reconciliation operations.

from .accounts import resolve_account  # Resolve GL accounts from entity-scoped codes.
from .audit import record  # Write finance audit events for reconciliation actions.
from .constants import (  # Import project symbols used by this module.
    BankLineStatus,  # Lifecycle of imported statement lines.
    BankMatchSource,  # Indicates whether a match was manual, automatic, or adjustment-driven.
    BankReconStatus,  # Overall reconciliation status snapshot.
    BankStatementStatus,  # Lifecycle of a bank statement.
    DocumentStatus,  # Posted/draft lifecycle for journal lines.
    FinanceAuditAction,  # Audit action enum for banking events.
    JournalSource,  # Journal origin indicator.
    NormalBalance,  # Controls how signed balances are interpreted.
)  # Close the grouped expression.
from .exceptions import BankReconciliationError  # Raised for reconciliation and matching violations.
from .posting import post_journal, resolve_period, reverse_journal  # Post, period-check, and reverse journals.


# --------------------------------------------------------------------------- #
# Statement import                                                            #
# --------------------------------------------------------------------------- #

def gl_account_balance(account) -> int:  # Define the callable used by this module.
    """Net posted balance of a GL ``account`` in kobo, signed to its normal side."""
    from django.db.models import Sum  # Aggregate debit and credit totals in SQL.
    from .models import AccountBalance  # Stored balance snapshot table.

    agg = AccountBalance.objects.filter(account=account).aggregate(  # Pull the posted debit/credit totals.
        d=Sum("debit_total"), c=Sum("credit_total"))
    net = (agg["d"] or 0) - (agg["c"] or 0)  # Compute the raw net balance.
    if account.normal_balance != NormalBalance.DEBIT:  # Flip sign when the account's normal side is credit.
        net = -net  # Store the intermediate module value.
    return int(net)  # Return a signed integer kobo balance.


@transaction.atomic  # Apply the decorator to this callable.
def import_statement_lines(bank_account, rows, *, statement_date=None, period_label="",
                           opening_balance=0, closing_balance=None, force=False, actor_user=None):  # Start the nested execution block.
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
    from .models import BankStatement, BankStatementLine  # Import lazily to avoid model cycles.

    rows = list(rows)  # Materialize the iterable so we can scan it once.
    created = []  # New statement lines to insert.
    suspected = []  # Duplicate-looking rows held back unless force is set.
    movement = 0  # Running signed movement across the imported batch.
    for row in rows:  # Normalize each raw row into a statement line.
        external_id = (row.get("external_id") or "").strip()  # Clean up the optional external id.
        if external_id and BankStatementLine.objects.filter(  # Skip exact duplicates by external id.
            bank_account=bank_account, external_id=external_id,  # Continue the structured value.
        ).exists():  # Start the nested execution block.
            continue  # Ignore already-imported rows with the same external id.
        amount = int(row["amount"])  # Normalize the signed amount to integer kobo.
        description = row.get("description", "")  # Optional bank memo/description.
        reference = row.get("reference", "")  # Optional bank reference.
        if (not force and not external_id and BankStatementLine.objects.filter(  # Heuristic duplicate detection when no external id exists.
            bank_account=bank_account, txn_date=row["txn_date"], amount=amount,
            description=description, reference=reference,  # Continue the structured value.
        ).exists()):  # Start the nested execution block.
            suspected.append({  # Continue the structured value.
                "txn_date": row["txn_date"], "amount": amount,
                "description": description, "reference": reference,
            })  # Execute the module statement.
            continue  # Hold suspicious duplicates back unless the caller forces the import.
        created.append(BankStatementLine(  # Create the unsaved statement line object.
            bank_account=bank_account,  # Continue the structured value.
            txn_date=row["txn_date"],
            amount=amount,  # Continue the structured value.
            description=description,  # Continue the structured value.
            reference=reference,  # Continue the structured value.
            external_id=external_id,  # Continue the structured value.
        ))  # Execute the module statement.
        movement += amount  # Keep a running total of the signed statement movement.

    if not created:  # Nothing new survived the dedupe checks.
        return None, [], suspected  # Return the computed module result.

    opening_balance = int(opening_balance or 0)  # Normalize the provided opening balance.
    if closing_balance is None:  # Derive the closing balance when the caller did not supply one.
        closing_balance = opening_balance + movement  # Opening plus signed movements equals closing.
    statement = BankStatement.objects.create(  # Create the imported statement header.
        bank_account=bank_account,  # Continue the structured value.
        statement_date=statement_date or max(l.txn_date for l in created),  # Continue the structured value.
        period_label=period_label or "",
        opening_balance=opening_balance, closing_balance=int(closing_balance),  # Continue the structured value.
        status=BankStatementStatus.UPLOADED, imported_by=actor_user,  # Continue the structured value.
    )  # Close the grouped expression.
    for line in created:  # Attach the new rows to the statement header before saving.
        line.statement = statement  # Set the foreign key on each line.
    BankStatementLine.objects.bulk_create(created)  # Bulk insert the imported statement lines.
    return statement, list(BankStatementLine.objects.filter(statement=statement)), suspected  # Return the statement and stored rows.


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #

#: Largest "one bank line covers N receipts" group auto-match tries.
GROUP_AUTO_MATCH_MAX = 4  # Store the intermediate module value.
#: Skip group auto-match when a statement line has more candidate GL lines than this
#: (keeps the bounded subset search cheap and the result unambiguous).
_GROUP_AUTO_MATCH_POOL_CAP = 12  # Store the intermediate module value.


def _signed_gl(line) -> int:  # Define the callable used by this module.
    """A cash-account journal line's signed contribution in kobo (debit - credit)."""
    return (line.debit or 0) - (line.credit or 0)  # Positive means cash in; negative means cash out.


def _unique_summing_subset(lines, target, *, max_size):  # Define the callable used by this module.
    """The **unique** subset (size 2..``max_size``) of ``lines`` whose signed amounts
    sum to ``target`` — or ``None`` when there is no such subset **or more than one**
    (ambiguous). Conservative on purpose: auto-grouping only fires when there's a single
    unambiguous answer.
    """
    from itertools import combinations  # Generate subsets to test sums.

    found = None  # Hold the single unambiguous subset when one exists.
    for size in range(2, min(max_size, len(lines)) + 1):  # Search subsets from size 2 up to the limit.
        for combo in combinations(lines, size):  # Try every combination of the current size.
            if sum(_signed_gl(gl) for gl in combo) == target:  # Check whether the subset sums exactly.
                if found is not None:  # More than one answer means the match is ambiguous.
                    return None  # Reject ambiguous group matches.
                found = combo  # Save the first unique matching subset.
    return found  # Return the unique subset or None when no answer exists.


def _unmatched_gl_lines(bank_account):  # Define the callable used by this module.
    """Posted cash-account journal lines not yet paired to a statement line."""
    from .models import JournalLine  # Import lazily to avoid model cycles.

    return (  # Find posted cash-account journal lines that are still unpaired.
        JournalLine.objects  # Execute the module statement.
        .filter(  # Continue the structured value.
            account=bank_account.gl_account,  # Continue the structured value.
            entry__status=DocumentStatus.POSTED,  # Continue the structured value.
        )  # Close the grouped expression.
        # Not paired either 1:1 (matched_line) or as part of a group match.  # Exclude already matched lines.
        .filter(bank_statement_lines__isnull=True, bank_line_matches__isnull=True)  # Store the intermediate module value.
        .select_related("entry")
        .order_by("entry__date", "id")
    )  # Close the grouped expression.


@transaction.atomic  # Apply the decorator to this callable.
def auto_reconcile(bank_account, *, tolerance_days=4, group=True,  # Define the callable used by this module.
                   max_group=GROUP_AUTO_MATCH_MAX, actor_user=None):  # Start the nested execution block.
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
    from .models import BankLineMatch, BankStatementLine  # Load reconciliation models lazily.

    pending = list(  # Statement lines that still need matching.
        BankStatementLine.objects  # Execute the module statement.
        .filter(bank_account=bank_account, status=BankLineStatus.UNMATCHED)  # Store the intermediate module value.
        .order_by("txn_date", "id")
    )  # Close the grouped expression.
    gl_lines = list(_unmatched_gl_lines(bank_account))  # Unpaired GL cash lines eligible for matching.
    consumed: set[int] = set()  # GL line ids already consumed by a match.
    matched = []  # Statement lines matched by this run.

    def _mark(sline):  # Define the callable used by this module.
        sline.status = BankLineStatus.MATCHED  # Mark the statement line matched.
        sline.match_source = BankMatchSource.AUTO  # Record that the match was automatic.
        sline.reconciled_at = timezone.now()  # Timestamp the match.

    for sline in pending:  # First pass: exact 1:1 amount matching.
        candidates = [  # Find all GL lines that match by amount and date window.
            gl for gl in gl_lines  # Execute the module statement.
            if gl.id not in consumed  # Branch on the current domain condition.
            and _signed_gl(gl) == sline.amount  # Execute the module statement.
            and abs((gl.entry.date - sline.txn_date).days) <= tolerance_days  # Execute the module statement.
        ]  # Close the grouped expression.
        if len(candidates) != 1:  # Ambiguous or absent matches stay for manual review.
            continue  # 0 = no match; >1 = ambiguous, leave it for a human
        gl = candidates[0]  # The only unambiguous candidate.
        sline.matched_line = gl  # Link the statement line to the GL line.
        _mark(sline)  # Mark the line as automatically matched.
        sline.save(update_fields=["matched_line", "status", "match_source", "reconciled_at", "updated_at"])  # Persist the match.
        consumed.add(gl.id)  # Prevent this GL line from being reused.
        matched.append(sline)  # Record the match for the return value.

    if group:  # Optional second pass: group multiple GL lines into a single bank line.
        # Second pass: group several GL lines that SUM to a still-unmatched bank line.
        for sline in pending:  # Revisit only the still-unmatched statement lines.
            if sline.status != BankLineStatus.UNMATCHED:  # Skip anything already matched in pass one.
                continue  # Skip to the next loop iteration.
            pool = [  # Candidate GL pool for a possible group match.
                gl for gl in gl_lines  # Execute the module statement.
                if gl.id not in consumed  # Branch on the current domain condition.
                and (_signed_gl(gl) > 0) == (sline.amount > 0)  # same direction
                and abs((gl.entry.date - sline.txn_date).days) <= tolerance_days  # Execute the module statement.
            ]  # Close the grouped expression.
            if not (2 <= len(pool) <= _GROUP_AUTO_MATCH_POOL_CAP):  # Require a bounded candidate pool.
                continue  # Skip to the next loop iteration.
            subset = _unique_summing_subset(pool, sline.amount, max_size=max_group)  # Find a unique subset sum.
            if subset is None:  # Ambiguous or absent group match.
                continue  # Skip to the next loop iteration.
            BankLineMatch.objects.bulk_create(  # Create one row per paired statement/journal line.
                [BankLineMatch(statement_line=sline, journal_line=gl) for gl in subset],  # Continue the structured value.
            )  # Close the grouped expression.
            _mark(sline)  # Mark the statement line matched.
            sline.save(update_fields=["status", "match_source", "reconciled_at", "updated_at"])  # Persist the group match.
            consumed.update(gl.id for gl in subset)  # Prevent reused GL lines.
            matched.append(sline)  # Include the matched statement line in the result.

    if matched:  # Emit an audit event only when something matched.
        record(  # Log the automatic reconciliation run.
            entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,  # Continue the structured value.
            actor_user=actor_user, target=bank_account,  # Continue the structured value.
            message=f"Auto-matched {len(matched)} line(s) on {bank_account.name}.",
            matched=len(matched), bank_account_id=bank_account.id,  # Continue the structured value.
        )  # Close the grouped expression.
        _record_reconciliation(bank_account, matched_count=len(matched), actor_user=actor_user)  # Snapshot the new state.
    return matched  # Return the statement lines that matched in this run.


def statement_balance(bank_account) -> int | None:  # Define the callable used by this module.
    """The most recent imported statement's closing balance (kobo), or None."""
    latest = bank_account.statements.order_by("-statement_date", "-id").first()  # Pick the latest statement.
    return int(latest.closing_balance) if latest else None  # Return its closing balance or None when absent.


def _record_reconciliation(bank_account, *, matched_count, actor_user=None):  # Define the callable used by this module.
    """Snapshot book vs statement balance after a reconcile, and close clean statements."""
    from .models import BankReconciliation  # Snapshot model for reconciliation results.

    book = gl_account_balance(bank_account.gl_account)  # Current GL cash balance.
    stmt = statement_balance(bank_account)  # Latest statement closing balance.
    stmt_val = stmt if stmt is not None else book  # Fall back to book balance when no statement exists.
    difference = book - stmt_val  # Difference between ledger and statement.
    recon = BankReconciliation.objects.create(  # Persist the reconciliation snapshot.
        bank_account=bank_account, as_of_date=timezone.now().date(),  # Continue the structured value.
        book_balance=book, statement_balance=stmt_val, difference=difference,  # Continue the structured value.
        matched_count=matched_count,  # Continue the structured value.
        status=BankReconStatus.BALANCED if difference == 0 else BankReconStatus.OUT_OF_BALANCE,  # Continue the structured value.
        performed_by=actor_user,  # Continue the structured value.
        statement=bank_account.statements.order_by("-statement_date", "-id").first(),
    )  # Close the grouped expression.
    # A statement with no remaining unmatched lines is fully reconciled.  # Mark clean statements reconciled.
    for st in bank_account.statements.filter(status=BankStatementStatus.UPLOADED):  # Check each uploaded statement.
        if not st.lines.filter(status=BankLineStatus.UNMATCHED).exists() and st.lines.exists():  # Fully matched statement.
            st.status = BankStatementStatus.RECONCILED  # Promote it to reconciled.
            st.save(update_fields=["status", "updated_at"])  # Persist the new status.
    return recon  # Return the reconciliation snapshot.


@transaction.atomic  # Apply the decorator to this callable.
def complete_reconciliation(bank_account, *, actor_user=None):  # Define the callable used by this module.
    """Finalise a reconciliation — record a snapshot of the account's current state."""
    from .models import BankStatementLine  # Statement line model for the matched count.

    matched = BankStatementLine.objects.filter(  # Count already-matched lines on this bank account.
        bank_account=bank_account, status=BankLineStatus.MATCHED).count()  # Store the intermediate module value.
    recon = _record_reconciliation(bank_account, matched_count=matched, actor_user=actor_user)  # Snapshot the state.
    record(  # Log the reconciliation completion.
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,  # Continue the structured value.
        actor_user=actor_user, target=bank_account,  # Continue the structured value.
        message=f"Reconciliation completed on {bank_account.name} "
                f"(diff {recon.difference} kobo).",
        bank_account_id=bank_account.id, difference=recon.difference,  # Continue the structured value.
    )  # Close the grouped expression.
    return recon  # Return the completed reconciliation snapshot.


@transaction.atomic  # Apply the decorator to this callable.
def match_line(statement_line, journal_line, *, actor_user=None):  # Define the callable used by this module.
    """Manually pair a statement line to a specific cash-account journal line."""
    bank_account = statement_line.bank_account  # Resolve the owning bank account once.
    if journal_line.account_id != bank_account.gl_account_id:  # The journal must sit on the cash account.
        raise BankReconciliationError(  # Raise the domain error for this path.
            "The journal line is not on this bank account's GL cash account.",
        )  # Close the grouped expression.
    if _signed_gl(journal_line) != statement_line.amount:  # Amounts must match exactly in signed form.
        raise BankReconciliationError(  # Raise the domain error for this path.
            f"Amount mismatch: statement {statement_line.amount} kobo vs journal line "
            f"{_signed_gl(journal_line)} kobo.",
        )  # Close the grouped expression.
    statement_line.matched_line = journal_line  # Link the manual match target.
    statement_line.status = BankLineStatus.MATCHED  # Mark the line reconciled.
    statement_line.match_source = BankMatchSource.MANUAL  # Record the manual source.
    statement_line.reconciled_at = timezone.now()  # Timestamp the match.
    statement_line.save(  # Persist the manual match fields.
        update_fields=["matched_line", "status", "match_source", "reconciled_at", "updated_at"],
    )  # Close the grouped expression.
    return statement_line  # Return the matched statement line.


@transaction.atomic  # Apply the decorator to this callable.
def group_match(statement_line, journal_lines, *, actor_user=None):  # Define the callable used by this module.
    """Match one statement line to **several** cash journal lines that sum to its amount.

    The many-to-one case: one bank line (e.g. a PSP settlement) settling multiple
    ledger movements. Each line must be posted, on this bank's GL cash account, and not
    already matched (1:1 or in another group); their signed amounts
    (``debit − credit``) must total the statement line's signed amount exactly. Records
    a :class:`~vs_finance.models.BankLineMatch` per pair — no ledger effect. Returns the
    statement line.
    """
    from .models import BankLineMatch  # Join table for group matches.

    if statement_line.status != BankLineStatus.UNMATCHED:  # Only unmatched lines can be grouped.
        raise BankReconciliationError(  # Raise the domain error for this path.
            f"Statement line is '{statement_line.status}', only an unmatched line can be matched.",
        )  # Close the grouped expression.
    lines = list(journal_lines)  # Materialize the candidate journal lines.
    if len(lines) < 2:  # Group matching needs more than one line.
        raise BankReconciliationError(  # Raise the domain error for this path.
            "A group match needs at least two journal lines (use match for a single line).",
        )  # Close the grouped expression.

    bank_account = statement_line.bank_account  # Resolve the owning bank account once.
    seen: set[int] = set()  # Prevent duplicate journal lines in the same group.
    total = 0  # Running signed total of the candidate lines.
    for jl in lines:  # Validate every candidate journal line.
        if jl.id in seen:  # Reject duplicates in the input list.
            raise BankReconciliationError("A journal line appears twice in the group.")
        seen.add(jl.id)  # Mark this journal line as seen.
        if jl.account_id != bank_account.gl_account_id:  # Every line must be on the cash account.
            raise BankReconciliationError(  # Raise the domain error for this path.
                "A journal line is not on this bank account's GL cash account.",
            )  # Close the grouped expression.
        if jl.entry.status != DocumentStatus.POSTED:  # Only posted journal lines can be matched.
            raise BankReconciliationError("Only posted journal lines can be matched.")
        if jl.bank_statement_lines.exists() or jl.bank_line_matches.exists():  # Reject already-matched lines.
            raise BankReconciliationError(f"Journal line {jl.id} is already matched.")
        total += _signed_gl(jl)  # Accumulate the signed contribution.

    if total != statement_line.amount:  # The group must sum exactly to the statement amount.
        raise BankReconciliationError(  # Raise the domain error for this path.
            f"Group total {total} kobo does not equal the statement line "
            f"{statement_line.amount} kobo.",
        )  # Close the grouped expression.

    BankLineMatch.objects.bulk_create(  # Persist one link row per journal line.
        [BankLineMatch(statement_line=statement_line, journal_line=jl) for jl in lines],  # Continue the structured value.
    )  # Close the grouped expression.
    statement_line.status = BankLineStatus.MATCHED  # Mark the statement line reconciled.
    statement_line.match_source = BankMatchSource.MANUAL  # Record that a human applied the group match.
    statement_line.reconciled_at = timezone.now()  # Timestamp the match.
    statement_line.save(  # Persist the statement line state.
        update_fields=["status", "match_source", "reconciled_at", "updated_at"],
    )  # Close the grouped expression.
    record(  # Write a reconciliation audit event.
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,  # Continue the structured value.
        actor_user=actor_user, target=bank_account,  # Continue the structured value.
        message=f"Group-matched a statement line to {len(lines)} journal line(s) "
                f"on {bank_account.name}.",
        bank_account_id=bank_account.id, journal_lines=len(lines),  # Continue the structured value.
    )  # Close the grouped expression.
    return statement_line  # Return the matched statement line.


@transaction.atomic  # Apply the decorator to this callable.
def split_match(journal_line, statement_lines, *, actor_user=None):  # Define the callable used by this module.
    """Match **one** cash journal line to **several** statement lines that sum to it.

    The reverse of :func:`group_match`: one ledger movement the bank reported as several
    lines (e.g. a payout split into principal + fee). Each statement line must be
    unmatched and on the same bank account as the journal line's GL cash account; their
    signed amounts total the journal line's signed amount. Records a
    :class:`~vs_finance.models.BankLineMatch` per statement line — no ledger effect.
    Unmatching any one of them later frees just that line (see :func:`unmatch_line`).
    """
    from .models import BankLineMatch  # Join table for split matches.

    slines = list(statement_lines)  # Materialize the statement lines.
    if len(slines) < 2:  # Split matching needs more than one statement line.
        raise BankReconciliationError(  # Raise the domain error for this path.
            "A split match needs at least two statement lines (use match for one).",
        )  # Close the grouped expression.
    bank_account = slines[0].bank_account  # All statement lines must belong to the same bank account.
    if journal_line.account_id != bank_account.gl_account_id:  # The journal must sit on the same cash account.
        raise BankReconciliationError(  # Raise the domain error for this path.
            "The journal line is not on this bank account's GL cash account.",
        )  # Close the grouped expression.
    if journal_line.entry.status != DocumentStatus.POSTED:  # Only posted journal lines can be matched.
        raise BankReconciliationError("Only a posted journal line can be matched.")
    if journal_line.bank_statement_lines.exists() or journal_line.bank_line_matches.exists():  # Reject already-matched lines.
        raise BankReconciliationError(f"Journal line {journal_line.id} is already matched.")

    seen: set[int] = set()  # Prevent duplicate statement lines in the input.
    total = 0  # Running signed sum of the statement lines.
    for sl in slines:  # Validate every statement line.
        if sl.id in seen:  # Reject duplicates in the input list.
            raise BankReconciliationError("A statement line appears twice in the split.")
        seen.add(sl.id)  # Mark the statement line as seen.
        if sl.bank_account_id != bank_account.id:  # All lines must belong to the same bank account.
            raise BankReconciliationError("All statement lines must belong to the same bank account.")
        if sl.status != BankLineStatus.UNMATCHED:  # Only unmatched lines can participate.
            raise BankReconciliationError(  # Raise the domain error for this path.
                f"Statement line {sl.id} is '{sl.status}', only an unmatched line can be matched.",
            )  # Close the grouped expression.
        total += sl.amount  # Accumulate the statement amounts.

    if total != _signed_gl(journal_line):  # The split must equal the journal line exactly.
        raise BankReconciliationError(  # Raise the domain error for this path.
            f"Statement lines sum to {total} kobo, not the journal line's "
            f"{_signed_gl(journal_line)} kobo.",
        )  # Close the grouped expression.

    BankLineMatch.objects.bulk_create(  # Create one match row per statement line.
        [BankLineMatch(statement_line=sl, journal_line=journal_line) for sl in slines],  # Continue the structured value.
    )  # Close the grouped expression.
    now = timezone.now()  # Share one timestamp across the matched rows.
    for sl in slines:  # Mark each statement line matched.
        sl.status = BankLineStatus.MATCHED  # Update the status.
        sl.match_source = BankMatchSource.MANUAL  # Record manual intervention.
        sl.reconciled_at = now  # Apply the same reconciliation timestamp.
        sl.save(update_fields=["status", "match_source", "reconciled_at", "updated_at"])  # Persist the state.
    record(  # Log the split match in the audit trail.
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,  # Continue the structured value.
        actor_user=actor_user, target=bank_account,  # Continue the structured value.
        message=f"Split-matched journal line {journal_line.id} across {len(slines)} "
                f"statement line(s) on {bank_account.name}.",
        bank_account_id=bank_account.id, statement_lines=len(slines),  # Continue the structured value.
    )  # Close the grouped expression.
    return slines  # Return the matched statement lines.


@transaction.atomic  # Apply the decorator to this callable.
def unmatch_line(statement_line, *, actor_user=None):  # Define the callable used by this module.
    """Undo a match — and reverse the adjusting journal if the match created one.

    A plain match just drops the pairing (no ledger effect). A match that booked
    an adjusting journal reverses that journal (a mirror entry that nets to zero),
    so unmatching never silently leaves the ledger out of step.
    """
    if statement_line.status != BankLineStatus.MATCHED:  # Only matched lines can be reversed.
        raise BankReconciliationError("Only a matched line can be unmatched.")
    bank_account = statement_line.bank_account  # Resolve the owning bank account.
    adj = statement_line.adjusting_journal  # Capture any adjusting journal before clearing it.
    if adj is not None:  # Reverse the adjusting journal when one exists.
        reverse_journal(adj, actor_user=actor_user)  # Post a reversing journal to neutralize the adjustment.
    # Drop any group-match links (many-to-one); these carry no ledger effect.  # Remove many-to-one links.
    statement_line.line_matches.all().delete()  # Clear group-match links.
    statement_line.matched_line = None  # Remove the direct match link.
    statement_line.adjusting_journal = None  # Remove the adjustment link.
    statement_line.status = BankLineStatus.UNMATCHED  # Restore the unmatched status.
    statement_line.match_source = ""  # Clear the match source.
    statement_line.reconciled_at = None  # Clear the reconciliation timestamp.
    statement_line.save(update_fields=[  # Persist the unmatch state.
        "matched_line", "adjusting_journal", "status", "match_source",
        "reconciled_at", "updated_at",
    ])  # Execute the module statement.
    record(  # Log the unmatch in the audit trail.
        entity=bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,  # Continue the structured value.
        actor_user=actor_user, target=bank_account,  # Continue the structured value.
        message=f"Unmatched a statement line on {bank_account.name}"
                + (f" and reversed adjusting journal {adj.pk}." if adj else "."),
        bank_account_id=bank_account.id,  # Continue the structured value.
    )  # Close the grouped expression.
    return statement_line  # Return the unmatched statement line.


@transaction.atomic  # Apply the decorator to this callable.
def set_line_ignored(statement_line, *, ignored=True, reason="", actor_user=None):
    """Mark an unmatched statement line ``IGNORED`` (a known duplicate / opening-balance
    line), or revert an ignored line back to ``UNMATCHED``.

    Ignored lines carry no ledger effect and drop out of the unreconciled count (which
    only counts ``UNMATCHED``), so a statement of MATCHED + IGNORED lines can still
    reconcile. Only unmatched↔ignored transitions are allowed.
    """
    if ignored:  # Move an unmatched line into ignored state.
        if statement_line.status != BankLineStatus.UNMATCHED:  # Only unmatched lines can be ignored.
            raise BankReconciliationError(  # Raise the domain error for this path.
                f"Statement line is '{statement_line.status}', only an unmatched line can be ignored.",
            )  # Close the grouped expression.
        statement_line.status = BankLineStatus.IGNORED  # Mark the line ignored.
    else:  # Restore an ignored line back to unmatched.
        if statement_line.status != BankLineStatus.IGNORED:  # Only ignored lines can be restored.
            raise BankReconciliationError("Only an ignored line can be un-ignored.")
        statement_line.status = BankLineStatus.UNMATCHED  # Restore the unmatched state.
    statement_line.save(update_fields=["status", "updated_at"])  # Persist the status change.
    record(  # Log the ignore/un-ignore action.
        entity=statement_line.bank_account.entity, action=FinanceAuditAction.BANK_RECONCILED,  # Continue the structured value.
        actor_user=actor_user, target=statement_line.bank_account,  # Continue the structured value.
        message=(f"{'Ignored' if ignored else 'Un-ignored'} a statement line on "
                 f"{statement_line.bank_account.name}." + (f" Reason: {reason}" if reason else "")),
        bank_account_id=statement_line.bank_account_id,  # Continue the structured value.
    )  # Close the grouped expression.
    return statement_line  # Return the updated statement line.


# --------------------------------------------------------------------------- #
# Adjusting journals (book what the statement reveals)                        #
# --------------------------------------------------------------------------- #

@transaction.atomic  # Apply the decorator to this callable.
def post_bank_adjustment(statement_line, *, counter_account=None, counter_code=None,  # Define the callable used by this module.
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
    from .constants import BANK_CHARGES_CODE  # Default code for bank charges.
    from .models import JournalEntry, JournalLine  # Journal models for the adjustment posting.

    bank_account = statement_line.bank_account  # Resolve the owning bank account.
    entity = bank_account.entity  # Resolve the owning entity.
    if statement_line.status != BankLineStatus.UNMATCHED:  # Only unmatched lines can be adjusted.
        raise BankReconciliationError(  # Raise the domain error for this path.
            f"Statement line is '{statement_line.status}', only an unmatched line can be adjusted.",
        )  # Close the grouped expression.
    if statement_line.amount == 0:  # Zero-amount lines do not require adjustments.
        raise BankReconciliationError("Cannot adjust a zero-amount statement line.")

    if counter_account is None:  # Resolve a default counter account when one is not supplied.
        counter_account = resolve_account(  # Default to bank charges if the caller didn't specify a code.
            entity, counter_code or BANK_CHARGES_CODE, label="bank charge counter",
        )  # Close the grouped expression.

    period = resolve_period(entity, statement_line.txn_date)  # Find the open accounting period for the line date.
    cash = bank_account.gl_account  # The bank account's GL cash account.
    magnitude = abs(statement_line.amount)  # Use the absolute amount for the adjustment journal.
    inflow = statement_line.amount > 0  # Positive means bank credit/inflow.

    entry = JournalEntry.objects.create(  # Create the adjusting journal header.
        entity=entity, branch=bank_account.branch,  # Continue the structured value.
        date=statement_line.txn_date, period=period,  # Continue the structured value.
        source=JournalSource.BANK,  # Continue the structured value.
        narration=narration or statement_line.description or "Bank adjustment",
        reference=statement_line.reference, created_by=actor_user,  # Continue the structured value.
    )  # Close the grouped expression.
    if inflow:  # Bank credited us, so cash is debited and the counter account is credited.
        cash_line = JournalLine.objects.create(  # Debit the cash account for the inflow.
            entry=entry, account=cash, debit=magnitude, credit=0,  # Continue the structured value.
            description="Bank credit", line_no=1,
        )  # Close the grouped expression.
        JournalLine.objects.create(  # Credit the counter account for the offset.
            entry=entry, account=counter_account, debit=0, credit=magnitude,  # Continue the structured value.
            description=statement_line.description or "Bank credit", line_no=2,
        )  # Close the grouped expression.
    else:  # Bank charged us, so the counter account is debited and cash is credited.
        JournalLine.objects.create(  # Debit the counter account for the expense.
            entry=entry, account=counter_account, debit=magnitude, credit=0,  # Continue the structured value.
            description=statement_line.description or "Bank charge", line_no=1,
        )  # Close the grouped expression.
        cash_line = JournalLine.objects.create(  # Credit the cash account for the bank charge.
            entry=entry, account=cash, debit=0, credit=magnitude,  # Continue the structured value.
            description="Bank charge", line_no=2,
        )  # Close the grouped expression.

    post_journal(entry, actor_user=actor_user)  # Validate and post the adjusting journal.

    statement_line.adjusting_journal = entry  # Link the adjustment journal.
    statement_line.matched_line = cash_line  # Treat the generated cash line as the match target.
    statement_line.status = BankLineStatus.MATCHED  # Mark the statement line matched.
    statement_line.match_source = BankMatchSource.ADJUSTMENT  # Record that the match came from an adjustment.
    statement_line.reconciled_at = timezone.now()  # Timestamp the adjustment match.
    statement_line.save(update_fields=[  # Persist the adjusted statement line state.
        "adjusting_journal", "matched_line", "status", "match_source",
        "reconciled_at", "updated_at",
    ])  # Execute the module statement.

    record(  # Log the bank adjustment in the audit trail.
        entity=entity, action=FinanceAuditAction.BANK_CHARGE_POSTED,  # Continue the structured value.
        actor_user=actor_user, target=bank_account,  # Continue the structured value.
        message=f"Booked bank adjustment {magnitude} kobo on {bank_account.name}.",
        journal_id=entry.pk, amount=statement_line.amount,  # Continue the structured value.
    )  # Close the grouped expression.
    return entry  # Return the posted adjusting journal entry.
