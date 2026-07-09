"""Posting-layer guards.

The principle locked in Phase 0: **business rules are enforced where journals are
posted, not in the UI.** A request that somehow bypasses the screens (an API call, a
script, a webhook) must still be unable to post into a closed period or write an
unbalanced entry.

In Phase 0 the ``FiscalPeriod`` model does not exist yet, so these guards are
duck-typed: they operate on any object exposing a ``status`` (and optionally a
``label``). Phase 1 introduces the real model and the full ``post_journal`` service,
which will call these same guards — keeping the enforcement point singular.
"""
from __future__ import annotations  # Defer annotation evaluation during app import.

from typing import Iterable, Protocol  # Iterable for line sums; Protocol for period-like guard type.

from django.db import transaction  # Keeps posting/reversal writes atomic.
from django.utils import timezone  # Supplies posted/reversal dates and timestamps.

from .constants import (
    DocumentStatus,  # Journal lifecycle status enum.
    FinanceAuditAction,  # Audit action enum values.
    JournalSource,  # Journal source enum values.
    PERIOD_POSTING_BLOCKED,  # Period statuses that never accept postings.
    PERIOD_POSTING_RESTRICTED,  # Period statuses that require privileged posting.
    PeriodStatus,  # Fiscal period lifecycle status enum.
)
from .exceptions import (
    FinanceError,  # Base finance error caught for durable rejection audit.
    InactiveAccountError,  # Raised when a line account cannot receive postings.
    PeriodClosedError,  # Raised when posting period is blocked.
    PostingError,  # Generic posting domain error.
    UnbalancedJournalError,  # Raised when debit and credit totals differ.
)


class _PeriodLike(Protocol):  # Structural type for period guard inputs.
    status: str  # Period-like objects must expose a status.

    def __str__(self) -> str:  # pragma: no cover - structural typing only  # Human period label for errors.
        ...


def ensure_period_open(period: _PeriodLike, *, allow_restricted: bool = False) -> None:  # Guard posting period availability.
    """Raise :class:`PeriodClosedError` if ``period`` cannot accept a posting.

    Args:
        period: Any object with a ``status`` string drawn from
            :class:`~vs_finance.constants.PeriodStatus`.
        allow_restricted: When ``True``, soft-closed periods are permitted — used by
            privileged close-process auto-postings (depreciation, accruals). Ordinary
            postings pass ``False`` and are blocked from soft-closed periods too.

    A missing period (``None``) is treated as a hard error: nothing posts without a
    period.
    """
    if period is None:  # Nothing should post without a resolved accounting period.
        raise PeriodClosedError(period_label="<none>", status="missing")

    status = getattr(period, "status", None)  # Read status defensively from period-like object.
    label = str(period)  # Human-readable period label for errors.

    if status in PERIOD_POSTING_BLOCKED:  # Closed/locked periods reject all postings.
        raise PeriodClosedError(period_label=label, status=str(status))

    if status in PERIOD_POSTING_RESTRICTED and not allow_restricted:  # Soft-closed periods require privileged posting.
        raise PeriodClosedError(period_label=label, status=str(status))

    if status != PeriodStatus.OPEN and status not in PERIOD_POSTING_RESTRICTED:  # Anything unknown must fail closed.
        # Unknown / unset status — fail closed rather than guess.  # Prevent silent posting into invalid state.
        raise PeriodClosedError(period_label=label, status=str(status or "unknown"))


def _period_accepts_posting(period, *, allow_restricted: bool = False) -> bool:  # Non-raising period posting test.
    """Non-raising sibling of :func:`ensure_period_open`: can a posting land here?

    Mirrors the guard's logic without raising, so callers (e.g. reversal-date
    selection) can *test* a period and pick an alternative rather than fail.
    """
    if period is None:  # Missing period cannot accept postings.
        return False
    status = getattr(period, "status", None)  # Read status defensively.
    if status in PERIOD_POSTING_BLOCKED:  # Closed/locked periods reject postings.
        return False
    if status in PERIOD_POSTING_RESTRICTED:  # Restricted periods depend on caller privilege.
        return allow_restricted
    return status == PeriodStatus.OPEN  # Only open periods accept ordinary postings.


def ensure_balanced(debit_kobo: int, credit_kobo: int) -> None:  # Guard exact double-entry equality.
    """Raise :class:`UnbalancedJournalError` unless debits exactly equal credits.

    Amounts are integer kobo, so equality is exact — there is no rounding tolerance,
    and none is wanted: a ledger that is off by one kobo is wrong.
    """
    if debit_kobo != credit_kobo:  # Even one kobo imbalance is invalid.
        raise UnbalancedJournalError(debit=debit_kobo, credit=credit_kobo)


def sum_sides(lines: Iterable) -> tuple[int, int]:  # Sum debit and credit sides over journal-like lines.
    """Sum ``(total_debit, total_credit)`` in kobo over an iterable of journal lines.

    Each line is expected to expose integer ``debit`` and ``credit`` attributes
    (kobo). Lines are one-sided by convention (a line is a debit OR a credit), but
    this tolerates both being present and simply sums them.
    """
    total_debit = 0  # Running debit total in kobo.
    total_credit = 0  # Running credit total in kobo.
    for line in lines:  # Walk each supplied journal line.
        total_debit += getattr(line, "debit", 0) or 0  # Add debit amount, treating None as zero.
        total_credit += getattr(line, "credit", 0) or 0  # Add credit amount, treating None as zero.
    return total_debit, total_credit  # Return both exact integer totals.


# ---------------------------------------------------------------------------
# Phase 1 — posting services
# ---------------------------------------------------------------------------
#
# These are the ONLY supported way to make a journal affect balances. They run the
# Phase-0 guards (period open, balanced), update the denormalised per-period balances
# atomically, stamp the document POSTED, and write an authoritative finance audit row
# (see vs_finance.audit) in the SAME transaction. Posting is never done by flipping
# ``status`` by hand.


def resolve_period(entity, date):  # Find the fiscal period covering a document date.
    """Return the :class:`FiscalPeriod` for ``entity`` covering ``date``, or ``None``.

    Used by sub-ledger services (AR/AP) to attach a journal to the right period from a
    document date. ``None`` is returned when no period covers the date; the posting
    guard then fails closed, refusing to post a dateless/period-less entry.
    """
    from .models import FiscalPeriod  # Local import avoids model import cycles.

    return (  # Return matching period or None.
        FiscalPeriod.objects  # Start from fiscal period manager.
        .filter(entity=entity, start_date__lte=date, end_date__gte=date)  # Date must fall within period boundaries.
        .order_by("period_no")  # Stable order if data is malformed/overlapping.
        .first()  # Return one period or None.
    )


def _apply_to_balances(entry, *, sign: int) -> None:  # Apply or remove journal amounts from account balances.
    """Add (sign=+1) or remove (sign=-1) an entry's line amounts to per-period balances. 
    So the journal lines are the source of truth, and the denormalised balances are kept in step.
    The ``sign`` argument allows this to be used for both posting and unposting (reversing) journals.

    Maintains one :class:`AccountBalance` row per ``(account, period)``, the fast
    aggregate behind trial balances. Truth still lives in the immutable lines; this is
    a denormalised read model kept in step inside the same transaction as the post.
    """
    from .models import AccountBalance  # Denormalized per-period account aggregate model.

    period = entry.period  # already guarded to exist and be open  # Posting target period.
    for line in entry.lines.select_related("account").all():  # Apply every line in the journal.
        balance, _ = AccountBalance.objects.select_for_update().get_or_create(  # Lock/create aggregate row.
            account=line.account, period=period,  # One balance row per account and period.
        )
        balance.debit_total += sign * (line.debit or 0)  # Add/remove debit amount.
        balance.credit_total += sign * (line.credit or 0)  # Add/remove credit amount.
        balance.save(update_fields=["debit_total", "credit_total", "updated_at"])  # Persist aggregate totals.


def post_journal(entry, *, actor_user=None, allow_restricted: bool = False):  # Public journal posting wrapper.
    """Post a draft :class:`~vs_finance.models.JournalEntry`, making it affect balances.

    Thin wrapper around the atomic core (:func:`_post_journal_atomic`) that turns any
    :class:`~vs_finance.exceptions.FinanceError` into a **durable** rejection audit row
    before re-raising. The rejection must be logged *outside* the rolled-back posting
    transaction, which is why this layer sits above the ``@transaction.atomic`` core.

    Idempotent guard: re-posting an already-POSTED entry raises rather than
    double-counting. Returns the entry.
    """
    from .audit import record_rejection  # Durable rejection audit helper.

    try:  # Atomic core may roll back; wrapper logs rejection after rollback.
        return _post_journal_atomic(  # Perform the actual posting.
            entry, actor_user=actor_user, allow_restricted=allow_restricted,  # Actor and period privilege.
        )
    except FinanceError as exc:  # Finance-domain errors get durable rejection audit.
        record_rejection(  # Record failed posting attempt.
            entity=entry.entity,  # Entity being posted into.
            action=FinanceAuditAction.JOURNAL_POST_REJECTED,  # Audit action for rejected journal post.
            exc=exc, actor_user=actor_user, target=entry,  # Error, actor, and target context.
        )
        raise  # Preserve original posting exception.


@transaction.atomic
def _post_journal_atomic(entry, *, actor_user=None, allow_restricted: bool = False):  # Transactional journal posting implementation.
    """The posting work proper, all in one transaction.

    Steps:
      1. Guard the period is open (SOFT_CLOSED only when ``allow_restricted``).
      2. Guard the lines balance (Σdebits == Σcredits, exact kobo).
      3. Guard every line's account is active and postable.
      4. Apply the line amounts to the per-period :class:`AccountBalance` aggregates.
      5. Stamp the entry POSTED with ``posted_at``/``posted_by``.
      6. Write the authoritative ``JOURNAL_POSTED`` audit row — same commit as 4–5.
    """
    from .audit import record  # Audit helper for successful posting.
    from .models import JournalEntry  # Journal model used for row locking.

    # Serialise concurrent posts of the *same* entry: take a row lock and re-read the
    # status under it before doing anything. Without this, two requests can both pass
    # the status guard on a stale in-memory copy and each apply the lines to
    # AccountBalance — double-counting the ledger. The loser blocks here, then sees
    # POSTED and is rejected. (Document numbering is already lock-safe; posting was not.)
    locked_status = (  # Re-read status while holding the journal row lock.
        JournalEntry.objects.select_for_update()  # Lock this journal row.
        .values_list("status", flat=True).get(pk=entry.pk)  # Fetch only current status.
    )
    if locked_status == DocumentStatus.POSTED:  # Posted journals must not be posted twice.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is already posted.",
        )
    if locked_status in (DocumentStatus.REVERSED, DocumentStatus.CANCELLED):  # Terminal journals cannot post.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is '{locked_status}' and cannot be posted.",
        )

    ensure_period_open(entry.period, allow_restricted=allow_restricted)  # Guard period status.

    lines = list(entry.lines.select_related("account").all())  # Load all lines and accounts once.
    if not lines:  # A journal with no lines has no accounting substance.
        raise PostingError("A journal must have at least one line to post.")

    total_debit, total_credit = sum_sides(lines)  # Calculate exact debit and credit totals.
    ensure_balanced(total_debit, total_credit)  # Enforce double-entry equality.

    for line in lines:  # Validate every account touched by the journal.
        account = line.account  # Account on this line.
        if not (account.is_active and account.is_postable):  # Inactive/header accounts cannot post.
            raise InactiveAccountError(account_code=account.code)

    _apply_to_balances(entry, sign=+1)  # Add line amounts to per-period balances.

    entry.status = DocumentStatus.POSTED  # Mark journal posted.
    entry.posted_at = timezone.now()  # Stamp posting timestamp.
    entry.posted_by = actor_user  # Store posting actor.
    entry.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])  # Persist posting fields.

    record(  # Audit the successful journal post.
        entity=entry.entity,  # Entity posted into.
        action=FinanceAuditAction.JOURNAL_POSTED,  # Audit action.
        actor_user=actor_user, target=entry,  # Actor and target context.
        message=f"Posted into {entry.period}.",  # Human-readable audit message.
        after={"status": DocumentStatus.POSTED, "posted_at": entry.posted_at.isoformat()},  # Post-state snapshot.
        debit=total_debit, credit=total_credit,  # Structured totals.
    )
    return entry  # Return posted journal.


@transaction.atomic
def reverse_journal(entry, *, actor_user=None, date=None, allow_restricted: bool = False):  # Reverse a posted journal.
    """Reverse a posted journal by raising a mirror-image entry that nets it to zero.

    The original is left untouched on the record (marked REVERSED) and a new journal —
    debits and credits swapped — is created and posted, linked back via ``reverses``.
    This is the audit-correct way to undo: history is appended to, never edited.

    The reversing entry posts into ``date``'s period (defaults to the original's
    period). Returns the new reversing entry.
    """
    from .models import JournalEntry, JournalLine  # Journal models for reversal entry and lines.

    if entry.status != DocumentStatus.POSTED:  # Only posted journals can be reversed.
        raise PostingError(
            f"Only a posted journal can be reversed; {entry.document_number or entry.pk} "
            f"is '{entry.status}'.",
        )
    if hasattr(entry, "reversed_by") and entry.reversed_by is not None:  # Prevent duplicate reversals.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} has already been reversed.",
        )

    # Resolve the reversal's date and period from that date (an old bug pinned the
    # reversal to the original's period regardless of the date passed). Prefer the
    # caller's date; else the original's. But if the original's period has since
    # closed and no explicit date was given, fall back to today so a prior-period
    # correction can still be booked into the current open period — the standard way
    # to reverse after a period closes.
    reversal_date = date or entry.date  # Prefer explicit reversal date, otherwise original date.
    period = resolve_period(entry.entity, reversal_date)  # Resolve period for selected reversal date.
    if date is None and not _period_accepts_posting(period, allow_restricted=allow_restricted):  # Original period may now be closed.
        reversal_date = timezone.now().date()  # Fall back to today for current-period correction.
        period = resolve_period(entry.entity, reversal_date)  # Resolve today's period.

    reversal = JournalEntry.objects.create(  # Create mirror journal header.
        entity=entry.entity,  # Same entity as original.
        branch=entry.branch,  # Same branch as original.
        date=reversal_date,  # Reversal accounting date.
        period=period,  # Reversal posting period.
        source=JournalSource.SYSTEM,  # System-generated reversal.
        currency=entry.currency,  # Same currency as original.
        fx_rate=entry.fx_rate,  # Same FX rate as original.
        narration=f"Reversal of {entry.document_number or entry.pk}",  # Clear reversal narration.
        reference=entry.reference,  # Preserve reference.
        created_by=actor_user,  # Attribute reversal to actor.
        reverses=entry,  # Link reversal back to original.
    )
    for line in entry.lines.all():  # Mirror every original journal line.
        JournalLine.objects.create(  # Create swapped-side reversal line.
            entry=reversal,  # Attach to reversal journal.
            account=line.account,  # Same account as original line.
            debit=line.credit,   # swap sides  # Original credit becomes reversal debit.
            credit=line.debit,  # Original debit becomes reversal credit.
            description=f"Reversal: {line.description}".strip(": "),  # Label reversal line.
            cost_center=line.cost_center,  # Preserve analytics.
            dimensions=line.dimensions,  # Preserve dimensions.
            line_no=line.line_no,  # Preserve line order.
        )

    post_journal(reversal, actor_user=actor_user, allow_restricted=allow_restricted)  # Post mirror journal.

    entry.status = DocumentStatus.REVERSED  # Mark original as reversed.
    entry.save(update_fields=["status", "updated_at"])  # Persist original status.

    from .audit import record  # Local import keeps audit dependency close to use.
    record(  # Audit reversal of original journal.
        entity=entry.entity,  # Entity context.
        action=FinanceAuditAction.JOURNAL_REVERSED,  # Audit action.
        actor_user=actor_user, target=entry,  # Actor and original journal target.
        message=f"Reversed by {reversal.document_number or reversal.pk}.",  # Human-readable audit message.
        after={"status": DocumentStatus.REVERSED},  # Original journal final status.
        reversal_id=reversal.pk, reversal_number=reversal.document_number,  # Link to reversal journal.
    )
    return reversal  # Return the posted reversal journal.


@transaction.atomic
def post_direct_entry(entity, *, lines, date=None, narration="", reference="",
                      actor_user=None):  # Create and post a raw direct journal entry.
    """Post a direct journal entry — money/balances seated into the books with no source doc.

    This is the *sanctioned* way to record anything that has no sub-ledger document behind
    it: capital injections and equity contributions, loan drawdowns, grants, opening cash,
    opening AR/AP, and manual adjustments. Unlike sub-ledger postings (which derive their
    journal from an invoice/payment/etc.), a direct entry is the one place a caller supplies
    raw lines. It posts with ``source=OPENING`` (the catch-all for sourceless entries).

    ``lines`` is a list of ``(account, debit_kobo, credit_kobo)`` — optionally extended
    with a 4th element ``cost_center`` and a 5th element ``dimensions`` — where ``account``
    is a code string (resolved within ``entity``) or an :class:`~vs_finance.models.Account`,
    the optional ``cost_center`` is a :class:`~vs_finance.models.CostCenter` (or ``None``)
    and ``dimensions`` is a ``{axis_code: value}`` map, both carried onto the GL line. The
    entry must balance (Σdebits == Σcredits); it posts into
    ``date``'s open period — ``date`` defaults to the entity's earliest period start, else
    today. The normal :func:`post_journal` guards apply (period open, balanced, accounts
    active/postable), and it is reversible like any journal. Returns the posted entry.
    """
    from django.utils import timezone  # Local import for default direct-entry date.

    from .accounts import resolve_account  # Resolves account codes to account objects.
    from .models import FiscalPeriod, JournalEntry, JournalLine  # Models needed to create the entry.

    rows = list(lines or [])  # Normalize iterable input and handle None.
    if not rows:  # Direct entries must include at least one line.
        raise PostingError("A direct entry needs at least one line.")

    if date is None:  # Default direct-entry date when caller omits one.
        date = (  # Prefer earliest fiscal period start, otherwise today.
            FiscalPeriod.objects.filter(entity=entity)  # Periods for this entity.
            .order_by("start_date").values_list("start_date", flat=True).first()  # Earliest available start date.
            or timezone.now().date()  # Fallback to today.
        )

    entry = JournalEntry.objects.create(  # Create draft direct journal header.
        entity=entity, date=date, period=resolve_period(entity, date),  # Entity, date, and resolved period.
        source=JournalSource.OPENING,  # Direct entries use opening/sourceless source.
        narration=narration or "Opening balances",  # Default narration.
        reference=reference, created_by=actor_user,  # External reference and actor.
    )
    for i, row in enumerate(rows, start=1):  # Create journal lines in input order.
        # optional 4th element: cost_center, optional 5th: dimensions JSON map  # Extra tuple values carry analytics.
        account, debit, credit, *rest = row  # Unpack mandatory and optional line values.
        cost_center = rest[0] if rest else None  # Optional cost center.
        dimensions = rest[1] if len(rest) > 1 else {}  # Optional dimensions JSON map.
        acct = account if not isinstance(account, str) else resolve_account(entity, account)  # Resolve code strings.
        JournalLine.objects.create(  # Create one journal line.
            entry=entry, account=acct,  # Attach line to entry and account.
            debit=int(debit or 0), credit=int(credit or 0),  # Store integer kobo side amounts.
            cost_center=cost_center, dimensions=dimensions or {}, line_no=i,  # Store analytics and line number.
        )

    post_journal(entry, actor_user=actor_user)  # Run normal posting guards and balance updates.
    entry.refresh_from_db()  # Reload posted fields and document number.
    return entry  # Return posted direct entry.
