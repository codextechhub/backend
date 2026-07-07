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
from __future__ import annotations

from typing import Iterable, Protocol

from django.db import transaction
from django.utils import timezone

from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    PERIOD_POSTING_BLOCKED,
    PERIOD_POSTING_RESTRICTED,
    PeriodStatus,
)
from .exceptions import (
    FinanceError,
    InactiveAccountError,
    PeriodClosedError,
    PostingError,
    UnbalancedJournalError,
)


class _PeriodLike(Protocol):
    status: str

    def __str__(self) -> str:  # pragma: no cover - structural typing only
        ...


def ensure_period_open(period: _PeriodLike, *, allow_restricted: bool = False) -> None:
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
    if period is None:
        raise PeriodClosedError(period_label="<none>", status="missing")

    status = getattr(period, "status", None)
    label = str(period)

    if status in PERIOD_POSTING_BLOCKED:
        raise PeriodClosedError(period_label=label, status=str(status))

    if status in PERIOD_POSTING_RESTRICTED and not allow_restricted:
        raise PeriodClosedError(period_label=label, status=str(status))

    if status != PeriodStatus.OPEN and status not in PERIOD_POSTING_RESTRICTED:
        # Unknown / unset status — fail closed rather than guess.
        raise PeriodClosedError(period_label=label, status=str(status or "unknown"))


def _period_accepts_posting(period, *, allow_restricted: bool = False) -> bool:
    """Non-raising sibling of :func:`ensure_period_open`: can a posting land here?

    Mirrors the guard's logic without raising, so callers (e.g. reversal-date
    selection) can *test* a period and pick an alternative rather than fail.
    """
    if period is None:
        return False
    status = getattr(period, "status", None)
    if status in PERIOD_POSTING_BLOCKED:
        return False
    if status in PERIOD_POSTING_RESTRICTED:
        return allow_restricted
    return status == PeriodStatus.OPEN


def ensure_balanced(debit_kobo: int, credit_kobo: int) -> None:
    """Raise :class:`UnbalancedJournalError` unless debits exactly equal credits.

    Amounts are integer kobo, so equality is exact — there is no rounding tolerance,
    and none is wanted: a ledger that is off by one kobo is wrong.
    """
    if debit_kobo != credit_kobo:
        raise UnbalancedJournalError(debit=debit_kobo, credit=credit_kobo)


def sum_sides(lines: Iterable) -> tuple[int, int]:
    """Sum ``(total_debit, total_credit)`` in kobo over an iterable of journal lines.

    Each line is expected to expose integer ``debit`` and ``credit`` attributes
    (kobo). Lines are one-sided by convention (a line is a debit OR a credit), but
    this tolerates both being present and simply sums them.
    """
    total_debit = 0
    total_credit = 0
    for line in lines:
        total_debit += getattr(line, "debit", 0) or 0
        total_credit += getattr(line, "credit", 0) or 0
    return total_debit, total_credit


# ---------------------------------------------------------------------------
# Phase 1 — posting services
# ---------------------------------------------------------------------------
#
# These are the ONLY supported way to make a journal affect balances. They run the
# Phase-0 guards (period open, balanced), update the denormalised per-period balances
# atomically, stamp the document POSTED, and write an authoritative finance audit row
# (see vs_finance.audit) in the SAME transaction. Posting is never done by flipping
# ``status`` by hand.


def resolve_period(entity, date):
    """Return the :class:`FiscalPeriod` for ``entity`` covering ``date``, or ``None``.

    Used by sub-ledger services (AR/AP) to attach a journal to the right period from a
    document date. ``None`` is returned when no period covers the date; the posting
    guard then fails closed, refusing to post a dateless/period-less entry.
    """
    from .models import FiscalPeriod

    return (
        FiscalPeriod.objects
        .filter(entity=entity, start_date__lte=date, end_date__gte=date)
        .order_by("period_no")
        .first()
    )


def _apply_to_balances(entry, *, sign: int) -> None:
    """Add (sign=+1) or remove (sign=-1) an entry's line amounts to per-period balances. 
    So the journal lines are the source of truth, and the denormalised balances are kept in step.
    The ``sign`` argument allows this to be used for both posting and unposting (reversing) journals.

    Maintains one :class:`AccountBalance` row per ``(account, period)``, the fast
    aggregate behind trial balances. Truth still lives in the immutable lines; this is
    a denormalised read model kept in step inside the same transaction as the post.
    """
    from .models import AccountBalance

    period = entry.period  # already guarded to exist and be open
    for line in entry.lines.select_related("account").all():
        balance, _ = AccountBalance.objects.select_for_update().get_or_create(
            account=line.account, period=period,
        )
        balance.debit_total += sign * (line.debit or 0)
        balance.credit_total += sign * (line.credit or 0)
        balance.save(update_fields=["debit_total", "credit_total", "updated_at"])


def post_journal(entry, *, actor_user=None, allow_restricted: bool = False):
    """Post a draft :class:`~vs_finance.models.JournalEntry`, making it affect balances.

    Thin wrapper around the atomic core (:func:`_post_journal_atomic`) that turns any
    :class:`~vs_finance.exceptions.FinanceError` into a **durable** rejection audit row
    before re-raising. The rejection must be logged *outside* the rolled-back posting
    transaction, which is why this layer sits above the ``@transaction.atomic`` core.

    Idempotent guard: re-posting an already-POSTED entry raises rather than
    double-counting. Returns the entry.
    """
    from .audit import record_rejection

    try:
        return _post_journal_atomic(
            entry, actor_user=actor_user, allow_restricted=allow_restricted,
        )
    except FinanceError as exc:
        record_rejection(
            entity=entry.entity,
            action=FinanceAuditAction.JOURNAL_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=entry,
        )
        raise


@transaction.atomic
def _post_journal_atomic(entry, *, actor_user=None, allow_restricted: bool = False):
    """The posting work proper, all in one transaction.

    Steps:
      1. Guard the period is open (SOFT_CLOSED only when ``allow_restricted``).
      2. Guard the lines balance (Σdebits == Σcredits, exact kobo).
      3. Guard every line's account is active and postable.
      4. Apply the line amounts to the per-period :class:`AccountBalance` aggregates.
      5. Stamp the entry POSTED with ``posted_at``/``posted_by``.
      6. Write the authoritative ``JOURNAL_POSTED`` audit row — same commit as 4–5.
    """
    from .audit import record
    from .models import JournalEntry

    # Serialise concurrent posts of the *same* entry: take a row lock and re-read the
    # status under it before doing anything. Without this, two requests can both pass
    # the status guard on a stale in-memory copy and each apply the lines to
    # AccountBalance — double-counting the ledger. The loser blocks here, then sees
    # POSTED and is rejected. (Document numbering is already lock-safe; posting was not.)
    locked_status = (
        JournalEntry.objects.select_for_update()
        .values_list("status", flat=True).get(pk=entry.pk)
    )
    if locked_status == DocumentStatus.POSTED:
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is already posted.",
        )
    if locked_status in (DocumentStatus.REVERSED, DocumentStatus.CANCELLED):
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is '{locked_status}' and cannot be posted.",
        )

    ensure_period_open(entry.period, allow_restricted=allow_restricted)

    lines = list(entry.lines.select_related("account").all())
    if not lines:
        raise PostingError("A journal must have at least one line to post.")

    total_debit, total_credit = sum_sides(lines)
    ensure_balanced(total_debit, total_credit)

    for line in lines:
        account = line.account
        if not (account.is_active and account.is_postable):
            raise InactiveAccountError(account_code=account.code)

    _apply_to_balances(entry, sign=+1)

    entry.status = DocumentStatus.POSTED
    entry.posted_at = timezone.now()
    entry.posted_by = actor_user
    entry.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])

    record(
        entity=entry.entity,
        action=FinanceAuditAction.JOURNAL_POSTED,
        actor_user=actor_user, target=entry,
        message=f"Posted into {entry.period}.",
        after={"status": DocumentStatus.POSTED, "posted_at": entry.posted_at.isoformat()},
        debit=total_debit, credit=total_credit,
    )
    return entry


@transaction.atomic
def reverse_journal(entry, *, actor_user=None, date=None, allow_restricted: bool = False):
    """Reverse a posted journal by raising a mirror-image entry that nets it to zero.

    The original is left untouched on the record (marked REVERSED) and a new journal —
    debits and credits swapped — is created and posted, linked back via ``reverses``.
    This is the audit-correct way to undo: history is appended to, never edited.

    The reversing entry posts into ``date``'s period (defaults to the original's
    period). Returns the new reversing entry.
    """
    from .models import JournalEntry, JournalLine

    if entry.status != DocumentStatus.POSTED:
        raise PostingError(
            f"Only a posted journal can be reversed; {entry.document_number or entry.pk} "
            f"is '{entry.status}'.",
        )
    if hasattr(entry, "reversed_by") and entry.reversed_by is not None:
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} has already been reversed.",
        )

    # Resolve the reversal's date and period from that date (an old bug pinned the
    # reversal to the original's period regardless of the date passed). Prefer the
    # caller's date; else the original's. But if the original's period has since
    # closed and no explicit date was given, fall back to today so a prior-period
    # correction can still be booked into the current open period — the standard way
    # to reverse after a period closes.
    reversal_date = date or entry.date
    period = resolve_period(entry.entity, reversal_date)
    if date is None and not _period_accepts_posting(period, allow_restricted=allow_restricted):
        reversal_date = timezone.now().date()
        period = resolve_period(entry.entity, reversal_date)

    reversal = JournalEntry.objects.create(
        entity=entry.entity,
        branch=entry.branch,
        date=reversal_date,
        period=period,
        source=JournalSource.SYSTEM,
        currency=entry.currency,
        fx_rate=entry.fx_rate,
        narration=f"Reversal of {entry.document_number or entry.pk}",
        reference=entry.reference,
        created_by=actor_user,
        reverses=entry,
    )
    for line in entry.lines.all():
        JournalLine.objects.create(
            entry=reversal,
            account=line.account,
            debit=line.credit,   # swap sides
            credit=line.debit,
            description=f"Reversal: {line.description}".strip(": "),
            cost_center=line.cost_center,
            dimensions=line.dimensions,
            line_no=line.line_no,
        )

    post_journal(reversal, actor_user=actor_user, allow_restricted=allow_restricted)

    entry.status = DocumentStatus.REVERSED
    entry.save(update_fields=["status", "updated_at"])

    from .audit import record
    record(
        entity=entry.entity,
        action=FinanceAuditAction.JOURNAL_REVERSED,
        actor_user=actor_user, target=entry,
        message=f"Reversed by {reversal.document_number or reversal.pk}.",
        after={"status": DocumentStatus.REVERSED},
        reversal_id=reversal.pk, reversal_number=reversal.document_number,
    )
    return reversal


@transaction.atomic
def post_direct_entry(entity, *, lines, date=None, narration="", reference="",
                      actor_user=None):
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
    from django.utils import timezone

    from .accounts import resolve_account
    from .models import FiscalPeriod, JournalEntry, JournalLine

    rows = list(lines or [])
    if not rows:
        raise PostingError("A direct entry needs at least one line.")

    if date is None:
        date = (
            FiscalPeriod.objects.filter(entity=entity)
            .order_by("start_date").values_list("start_date", flat=True).first()
            or timezone.now().date()
        )

    entry = JournalEntry.objects.create(
        entity=entity, date=date, period=resolve_period(entity, date),
        source=JournalSource.OPENING,
        narration=narration or "Opening balances",
        reference=reference, created_by=actor_user,
    )
    for i, row in enumerate(rows, start=1):
        # optional 4th element: cost_center, optional 5th: dimensions JSON map
        account, debit, credit, *rest = row
        cost_center = rest[0] if rest else None
        dimensions = rest[1] if len(rest) > 1 else {}
        acct = account if not isinstance(account, str) else resolve_account(entity, account)
        JournalLine.objects.create(
            entry=entry, account=acct,
            debit=int(debit or 0), credit=int(credit or 0),
            cost_center=cost_center, dimensions=dimensions or {}, line_no=i,
        )

    post_journal(entry, actor_user=actor_user)
    entry.refresh_from_db()
    return entry
