"""Posting-layer guards, and the service that runs them.

**Business rules are enforced where journals are posted, not in the UI.** A
request that bypasses the screens - an API call, a script, a webhook - must
still be unable to post into a closed period or write an unbalanced entry.

The guards are duck-typed rather than bound to :class:`~vs_finance.models.
FiscalPeriod`: they operate on any object exposing a ``status``, and optionally
a ``label`` for the error message. :func:`post_journal` calls these same guards,
which is what keeps the enforcement point singular.
"""
from __future__ import annotations

from typing import Iterable, Protocol

from django.db import transaction
from django.utils import timezone

from vs_config.clock import branch_today, tenant_today
from vs_config.display import format_date

from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
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


# Structural type for period guard inputs.
class _PeriodLike(Protocol):
    status: str  # Period-like objects must expose a status.

    # pragma: no cover - structural typing only  # Human period label for errors.
    def __str__(self) -> str:
        ...


#: Fiscal-year statuses that seal every period of the year, whatever the month says.
_SEALED_YEAR_STATUSES = (PeriodStatus.CLOSED, PeriodStatus.LOCKED)


def sealed_fiscal_year(period, *, fresh: bool = True):
    """Return ``(label, status)`` of ``period``'s fiscal year when it is sealed, else ``None``.

    A closed year has had its result rolled into Retained Earnings. A posting into
    any of its months, even one that reads OPEN or SOFT_CLOSED, would land outside
    that result for good, so the year's status is part of the posting guard.

    ``fresh`` reads the year's status from the database rather than from a
    related instance the caller may have loaded before the year closed. The
    non-raising sibling passes ``False`` and uses a loaded relation when there is
    one, so a screen listing every period asks once. The enforcing guard does not
    use this at all: it reads under a lock (:func:`read_key_shared`). Objects with
    no ``fiscal_year_id`` (the guard is duck-typed) have no year.
    """
    from .models import FiscalPeriod, FiscalYear

    year_id = getattr(period, "fiscal_year_id", None)
    if year_id is None:
        return None
    if not fresh and isinstance(period, FiscalPeriod) and FiscalPeriod.fiscal_year.is_cached(period):
        year = period.fiscal_year
        state = (year.year, year.status)
    else:
        state = FiscalYear.objects.filter(pk=year_id).values_list("year", "status").first()
    if state is None or state[1] not in _SEALED_YEAR_STATUSES:
        return None
    return f"FY{state[0]}", str(state[1])


def read_key_shared(model, pk, fields):
    """Read ``fields`` of one row, holding a FOR KEY SHARE lock on it until commit.

    The posting guard reads a period's and a fiscal year's status this way, inside
    the posting transaction, so a posting in flight and a close of that month or
    year serialise. The close takes FOR UPDATE on the same row, which conflicts
    with KEY SHARE, so it waits for every posting already past the guard and every
    later posting waits for it and then reads the status it committed.

    KEY SHARE is the weakest lock that does this. Postings take it on the same
    rows and never wait for each other, and it does not conflict with an ordinary
    UPDATE of the row either, so a closer that already holds FOR UPDATE can still
    change the status it locked. Django has no queryset API for it, hence the SQL.
    On a database other than PostgreSQL the row is read without a lock.

    Returns the values as a tuple in ``fields`` order, or ``None`` when no row has
    that primary key.
    """
    from django.db import connections, router

    connection = connections[router.db_for_write(model)]
    quote = connection.ops.quote_name
    columns = ", ".join(quote(model._meta.get_field(name).column) for name in fields)
    lock = " FOR KEY SHARE" if connection.vendor == "postgresql" else ""
    sql = (
        f"SELECT {columns} FROM {quote(model._meta.db_table)} "
        f"WHERE {quote(model._meta.pk.column)} = %s{lock}"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [pk])
        return cursor.fetchone()


def _refuse_period_status(label, status, *, allow_restricted, allow_closed):
    """Raise :class:`PeriodClosedError` unless a month in ``status`` takes the posting."""
    if status == PeriodStatus.LOCKED:  # A statutory lock is irreversible, even at year end.
        raise PeriodClosedError(period_label=label, status=str(status))

    if status == PeriodStatus.CLOSED:  # Only the formal year close may bypass CLOSED.
        if allow_closed:
            return
        raise PeriodClosedError(period_label=label, status=str(status))

    if status in PERIOD_POSTING_RESTRICTED and not allow_restricted:  # Soft-closed periods require privileged posting.
        raise PeriodClosedError(period_label=label, status=str(status))

    if status != PeriodStatus.OPEN and status not in PERIOD_POSTING_RESTRICTED:  # Anything unknown must fail closed.
        raise PeriodClosedError(period_label=label, status=str(status or "unknown"))


# Guard posting period availability.
def ensure_period_open(
    period: _PeriodLike,
    *,
    allow_restricted: bool = False,
    allow_closed: bool = False,
    branch=None,
) -> None:
    """Raise :class:`PeriodClosedError` if ``period`` cannot accept a posting.

    A period of a CLOSED or LOCKED fiscal year refuses every posting, whatever its
    own status and whatever the flags say. The year-end closing journal is no
    exception to this: :func:`vs_finance.close.close_fiscal_year` posts it while the
    year is still OPEN and seals the year afterwards, and
    :func:`vs_finance.close.reopen_fiscal_year` sets the year OPEN before it
    reverses that journal.

    For a saved :class:`~vs_finance.models.FiscalPeriod` the year's and the month's
    statuses are read from the database under a KEY SHARE lock
    (:func:`read_key_shared`), year first and month second, the order the year
    close takes its own locks in. Called inside the posting transaction, as
    :func:`post_journal` does, the locks hold until commit, so a close cannot slip
    between this check and the posting it guards. The status on the object passed
    in is checked as well, so a caller holding a stricter view is not overruled.

    Args:
        period: Any object with a ``status`` string drawn from
            :class:`~vs_finance.constants.PeriodStatus`.
        allow_restricted: When ``True``, soft-closed periods are permitted - used by
            privileged close-process auto-postings (depreciation, accruals). Ordinary
            postings pass ``False`` and are blocked from soft-closed periods too.
        allow_closed: A narrower year-end-close escape hatch. When ``True`` a CLOSED
            period may accept the formal closing journal, or its reversal when the
            year is reopened; LOCKED periods remain immutable. No ordinary or
            month-end posting should set this flag.

    A missing period (``None``) is treated as a hard error: nothing posts without a
    period.
    """
    from .models import BranchFiscalPeriod, BranchFiscalYear, FiscalPeriod, FiscalYear

    if period is None:  # Nothing should post without a resolved accounting period.
        raise PeriodClosedError(period_label="<none>", status="missing")

    status = getattr(period, "status", None)  # Read status defensively from period-like object.
    label = str(period)  # Human-readable period label for errors.

    year_id = getattr(period, "fiscal_year_id", None)
    if year_id is not None:  # Year first: the order the year close locks in.
        year = read_key_shared(FiscalYear, year_id, ("year", "status"))
        if year is not None and year[1] in _SEALED_YEAR_STATUSES:
            raise PeriodClosedError(
                period_label=label, status=str(year[1]), fiscal_year_label=f"FY{year[0]}",
            )

    if getattr(period, "is_closing", False) and not allow_closed:  # Year-end close only.
        raise PeriodClosedError(period_label=label, status="CLOSING")

    _refuse_period_status(
        label, status, allow_restricted=allow_restricted, allow_closed=allow_closed,
    )

    if isinstance(period, FiscalPeriod) and period.pk is not None:
        stored = read_key_shared(FiscalPeriod, period.pk, ("status",))
        if stored is not None and stored[0] != status:  # Committed status wins over a stale copy.
            _refuse_period_status(
                f"{period.name} [{stored[0]}]", stored[0],
                allow_restricted=allow_restricted, allow_closed=allow_closed,
            )
        if branch is not None and not period.is_closing:
            branch_id = getattr(branch, "pk", branch)
            branch_year = BranchFiscalYear.objects.filter(
                fiscal_year_id=period.fiscal_year_id, branch_id=branch_id,
            ).values_list("pk", flat=True).first()
            if branch_year is not None:
                year_status = read_key_shared(BranchFiscalYear, branch_year, ("status",))
                if year_status is not None:
                    _refuse_period_status(
                        f"FY{period.fiscal_year.year} branch {branch_id}", year_status[0],
                        allow_restricted=allow_restricted, allow_closed=allow_closed,
                    )
            branch_period = BranchFiscalPeriod.objects.filter(
                period=period, branch_id=branch_id,
            ).values_list("pk", flat=True).first()
            if branch_period is not None:
                branch_status = read_key_shared(BranchFiscalPeriod, branch_period, ("status",))
                if branch_status is not None:
                    _refuse_period_status(
                        f"{period.name} branch {branch_id}", branch_status[0],
                        allow_restricted=allow_restricted, allow_closed=allow_closed,
                    )


# Non-raising period posting test.
def _period_accepts_posting(
    period,
    *,
    allow_restricted: bool = False,
    allow_closed: bool = False,
) -> bool:
    """Non-raising sibling of :func:`ensure_period_open`: can a posting land here?

    Mirrors the guard's logic without raising, so callers (e.g. reversal-date
    selection) can *test* a period and pick an alternative rather than fail.
    """
    if period is None:  # Missing period cannot accept postings.
        return False
    if sealed_fiscal_year(period, fresh=False) is not None:  # A closed year takes nothing.
        return False
    status = getattr(period, "status", None)  # Read status defensively.
    if status == PeriodStatus.LOCKED:  # A locked period never accepts another entry.
        return False
    if status == PeriodStatus.CLOSED:  # CLOSED is bypassed only for a formal year-end journal.
        return allow_closed
    if status in PERIOD_POSTING_RESTRICTED:  # Restricted periods depend on caller privilege.
        return allow_restricted
    return status == PeriodStatus.OPEN  # Only open periods accept ordinary postings.


# Describe which dates an ordinary posting may use.
def posting_window(entity, *, today=None, branch=None) -> dict:
    """Return the dates ``entity`` will currently accept an ordinary posting on.

    The read-side mirror of :func:`ensure_period_open`: that guard answers "may this
    date post?" one date at a time after the fact, and this answers "which dates may
    post?" up front, so a screen can stop offering a date the guard would reject with
    a 409. Both read the same ``PeriodStatus``, so they cannot drift.

    ``open`` lists the OPEN periods (oldest first) as selectable ranges. ``blocked``
    lists the rest, so a picker can say *why* a date is unavailable rather than just
    greying it out. SOFT_CLOSED counts as blocked here: only privileged close-process
    postings may use it (``allow_restricted``), and this window describes what an
    ordinary user may pick. Every month of a CLOSED or LOCKED fiscal year is blocked
    too, whatever its own status, as the guard refuses it.

    ``default_date`` is the date a new document should open on: today when today is
    postable, else the nearest open day in either direction - backward to the most
    recent open day, or forward to the earliest upcoming one, whichever is closer,
    preferring the past on a tie (backdating into a still-open period is the ordinary
    case during a close; post-dating is not). ``None`` when the entity has no open
    period at all, which is a real state the caller must handle rather than paper over.
    """
    from .models import FiscalPeriod

    today = today or tenant_today(entity.tenant)
    periods = list(
        FiscalPeriod.objects
        .filter(entity=entity, is_closing=False)  # No date is picked in a closing period.
        .select_related("fiscal_year")
        .order_by("start_date", "period_no")
    )
    if branch is not None:
        from .models import BranchFiscalPeriod, BranchFiscalYear

        branch_id = getattr(branch, "pk", branch)
        states = dict(BranchFiscalPeriod.objects.filter(
            period__in=periods, branch_id=branch_id,
        ).values_list("period_id", "status"))
        year_states = dict(BranchFiscalYear.objects.filter(
            fiscal_year_id__in={period.fiscal_year_id for period in periods},
            branch_id=branch_id,
        ).values_list("fiscal_year_id", "status"))
        for period in periods:
            period.status = states.get(period.pk, period.status)
            period._branch_year_status = year_states.get(
                period.fiscal_year_id, period.fiscal_year.status,
            )

    open_periods = [
        p for p in periods
        if getattr(p, "_branch_year_status", PeriodStatus.OPEN) == PeriodStatus.OPEN
        and _period_accepts_posting(p)
    ]
    covering = next(  # The open period containing today, if any.
        (p for p in open_periods if p.start_date <= today <= p.end_date), None,
    )

    if covering is not None:
        default_date = today
    else:
        # Nearest open boundary on each side; the closer one wins, past on a tie.
        previous = max(
            (p.end_date for p in open_periods if p.end_date < today), default=None,
        )
        upcoming = min(
            (p.start_date for p in open_periods if p.start_date > today), default=None,
        )
        if previous and upcoming:
            default_date = (
                previous if (today - previous) <= (upcoming - today) else upcoming
            )
        else:
            default_date = previous or upcoming  # Whichever side exists (or None).

    return {
        "today": today,
        "today_is_open": covering is not None,
        "default_date": default_date,
        "default_period": _period_brief(
            next(
                (p for p in open_periods
                 if default_date and p.start_date <= default_date <= p.end_date),
                None,
            ),
        ),
        "open": [_period_brief(p) for p in open_periods],
        "blocked": [_period_brief(p) for p in periods if p not in open_periods],
    }


# How close to the end of the fiscal calendar counts as "running out", by default.
# An entity may choose its own lead (FinanceCalendarSettings.next_year_lead_days);
# this is the value it starts with. Two months is enough notice for a finance team
# to raise, review and create the next year without the request becoming an
# emergency.
FISCAL_RUNWAY_WARNING_DAYS = 60

# The three states an entity's fiscal calendar can be in, from the runway read.
FISCAL_RUNWAY_HEALTHY = "HEALTHY"    # Plenty of calendar left; nothing to say.
FISCAL_RUNWAY_EXPIRING = "EXPIRING"  # Coverage breaks within the warning threshold.
FISCAL_RUNWAY_EXPIRED = "EXPIRED"    # Today is not covered (lapsed, a gap, or no calendar).


def _coverage_runs(periods):
    """Merge period date ranges into unbroken ``[start, end]`` runs, oldest first.

    ``periods`` is an iterable of ``(start_date, end_date)``. Two ranges belong to
    one run when the second starts on or before the day after the first ends, so
    back-to-back months join and only a real uncovered day splits a run.
    """
    import datetime

    runs: list[list] = []
    for start, end in sorted(periods):
        if runs and start <= runs[-1][1] + datetime.timedelta(days=1):
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])
    return runs


# Describe how much fiscal calendar an entity has left.
def fiscal_calendar_runway(entity, *, today=None) -> dict:
    """Return when ``entity`` runs out of fiscal calendar, and whether to warn now.

    The other read-side mirror of the period guard, alongside :func:`posting_window`.
    That one answers "which dates may post *today*?"; this one answers "how long
    until the next date that *cannot* post?" - a question nothing else in the engine
    asks, and the reason it needs asking is that the answer arrives as a hard outage:

    :class:`~vs_finance.models.FiscalPeriod` rows are created a year at a time. Once
    the covered stretch ends with no period after it, :func:`resolve_period` returns
    ``None`` for every document dated there, :func:`ensure_period_open` raises
    :class:`~vs_finance.exceptions.PeriodClosedError` on the ``None``, and *every*
    posting on those dates fails at once - invoices, receipts, payroll, gateway
    webhooks, close journals. Nothing degrades first, so nothing warns unless we read
    ahead of the date deliberately.

    The stretch can end in two ways, and both are read: the last period's end, and a
    **gap** between two fiscal years. A January year followed by a September year
    leaves January to August uncovered; reading only the last end date would call
    that healthy while every posting in those months fails.

    * ``calendar_end`` is the last day any period covers, whatever its status: a
      CLOSED December still bounds the calendar.
    * ``first_uncovered_date`` is the first date on or after ``today`` that no period
      covers: the day postings start failing. It is ``today`` itself when today is
      already uncovered.
    * ``days_remaining`` counts from ``today`` to the last covered day before that
      break, and goes negative once today is past it, so a caller can say how long
      ago the calendar lapsed. It is ``None`` when nothing before today is covered.
    * ``gaps`` lists every uncovered stretch between two covered ones, oldest first.
    * ``threshold_days`` is the entity's own lead
      (:class:`~vs_finance.models.FinanceCalendarSettings`), defaulting to
      :data:`FISCAL_RUNWAY_WARNING_DAYS`.

    Three states, each real and each needing different words on screen:

    * ``HEALTHY`` - coverage runs beyond the threshold; say nothing.
    * ``EXPIRING`` - coverage breaks within the threshold; still posting, but
      somebody must create the missing year before that date.
    * ``EXPIRED`` - today itself is not covered, so nothing dated today can post. An
      entity with **no periods whatsoever** lands here too (with a ``None`` calendar
      end): a brand-new entity that was never given a calendar is in exactly the same
      can't-post position as one that ran off the end of its own, and treating it as
      an error would hide the very state the caller asked about.
    """
    import datetime

    from .calendar_settings import resolve_finance_calendar_settings
    from .models import FiscalPeriod

    today = today or tenant_today(entity.tenant)
    threshold = resolve_finance_calendar_settings(entity).next_year_lead_days
    last = (  # The period that bounds the calendar, ignoring status entirely.
        FiscalPeriod.objects
        .filter(entity=entity)
        .order_by("-end_date", "-period_no")
        .first()
    )
    runs = _coverage_runs(
        FiscalPeriod.objects.filter(entity=entity).values_list("start_date", "end_date"),
    )
    one_day = datetime.timedelta(days=1)

    calendar_end = last.end_date if last is not None else None
    covering = next((run for run in runs if run[0] <= today <= run[1]), None)
    if covering is not None:
        covered_until = covering[1]
        first_uncovered = covered_until + one_day
    else:
        first_uncovered = today
        covered_until = max((run[1] for run in runs if run[1] < today), default=None)
    days_remaining = (covered_until - today).days if covered_until is not None else None

    if covering is None:  # No calendar, a lapsed one, or today falls in a gap.
        status = FISCAL_RUNWAY_EXPIRED
    elif days_remaining <= threshold:  # Coverage breaks today or within notice.
        status = FISCAL_RUNWAY_EXPIRING
    else:
        status = FISCAL_RUNWAY_HEALTHY

    return {
        "today": today,
        "calendar_end": calendar_end,
        "first_uncovered_date": first_uncovered,
        "days_remaining": days_remaining,
        "threshold_days": threshold,
        "status": status,
        "should_warn": status != FISCAL_RUNWAY_HEALTHY,
        "last_period": _period_brief(last),
        "gaps": [
            {"start": earlier[1] + one_day, "end": later[0] - one_day}
            for earlier, later in zip(runs, runs[1:])
        ],
    }


# Shrink a period to the fields a date picker needs.
def _period_brief(period) -> dict | None:
    """Serialise a period to the minimum a picker needs: when it is and why."""
    if period is None:
        return None
    return {
        "id": period.id,
        "name": period.name,
        "period_no": period.period_no,
        "status": period.status,
        "start_date": period.start_date,
        "end_date": period.end_date,
    }


# Guard exact double-entry equality.
def ensure_balanced(debit_kobo: int, credit_kobo: int) -> None:
    """Raise :class:`UnbalancedJournalError` unless debits exactly equal credits.

    Amounts are integer kobo, so equality is exact - there is no rounding tolerance,
    and none is wanted: a ledger that is off by one kobo is wrong.
    """
    if debit_kobo != credit_kobo:  # Even one kobo imbalance is invalid.
        raise UnbalancedJournalError(debit=debit_kobo, credit=credit_kobo)


# Sum debit and credit sides over journal-like lines.
def sum_sides(lines: Iterable) -> tuple[int, int]:
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
# Posting services
# ---------------------------------------------------------------------------
#
# These are the ONLY supported way to make a journal affect balances. They run the
# Phase-0 guards (period open, balanced), update the denormalised per-period balances
# atomically, stamp the document POSTED, and write an authoritative finance audit row
# (see vs_finance.audit) in the SAME transaction. Posting is never done by flipping
# ``status`` by hand.


# Find the fiscal period covering a document date.
def resolve_period(entity, date):
    """Return the :class:`FiscalPeriod` for ``entity`` covering ``date``, or ``None``.

    Used by sub-ledger services (AR/AP) to attach a journal to the right period from a
    document date. ``None`` is returned when no period covers the date; the posting
    guard then fails closed, refusing to post a dateless/period-less entry.

    A year's closing period shares its last day with the last month, and is never
    the answer: the year's last day resolves to the ordinary month, so no posting
    reaches the closing period by its date. Only the year-end close names it.
    """
    from .models import FiscalPeriod

    return (  # Return matching period or None.
        FiscalPeriod.objects
        .filter(entity=entity, start_date__lte=date, end_date__gte=date, is_closing=False)
        .order_by("period_no")
        .first()
    )


# Apply or remove journal amounts from account balances.
def _apply_to_balances(entry, *, sign: int) -> None:
    """Add (sign=+1) or remove (sign=-1) an entry's line amounts to per-period balances. 
    So the journal lines are the source of truth, and the denormalised balances are kept in step.
    The ``sign`` argument allows this to be used for both posting and unposting (reversing) journals.

    Maintains one :class:`AccountBalance` row per ``(account, period)``, the fast
    aggregate behind trial balances. Truth still lives in the immutable lines; this is
    a denormalised read model kept in step inside the same transaction as the post.
    """
    from .models import AccountBalance

    period = entry.period  # already guarded to exist and be open  # Posting target period.
    for line in entry.lines.select_related("account").all():
        balance, _ = AccountBalance.objects.select_for_update().get_or_create(
            account=line.account, period=period,  # One balance row per account and period.
        )
        balance.debit_total += sign * (line.debit or 0)  # Add/remove debit amount.
        balance.credit_total += sign * (line.credit or 0)  # Add/remove credit amount.
        balance.save(update_fields=["debit_total", "credit_total", "updated_at"])


# Public journal posting wrapper.
def post_journal(
    entry,
    *,
    actor_user=None,
    allow_restricted: bool = False,
    allow_closed: bool = False,
    allow_control_accounts: bool = False,
):
    """Post a draft :class:`~vs_finance.models.JournalEntry`, making it affect balances.

    Thin wrapper around the atomic core (:func:`_post_journal_atomic`) that turns any
    :class:`~vs_finance.exceptions.FinanceError` into a **durable** rejection audit row
    before re-raising. The rejection must be logged *outside* the rolled-back posting
    transaction, which is why this layer sits above the ``@transaction.atomic`` core.

    A journal whose source says a person typed it (``MANUAL`` or ``OPENING``) may not
    touch an account a sub-ledger keeps (:mod:`vs_finance.control_accounts`).
    ``allow_control_accounts`` is for the one kind of sub-ledger document that posts an
    ``OPENING`` journal to a control account on purpose: an opening balance carried by
    its own document, such as an opening supplier bill.

    Idempotent guard: re-posting an already-POSTED entry raises rather than
    double-counting. Returns the entry.
    """
    from .audit import record_rejection

    try:  # Atomic core may roll back; wrapper logs rejection after rollback.
        return _post_journal_atomic(  # Perform the actual posting.
            entry, actor_user=actor_user, allow_restricted=allow_restricted,
            allow_closed=allow_closed,  # Actor and tightly scoped period privileges.
            allow_control_accounts=allow_control_accounts,
        )
    except FinanceError as exc:  # Finance-domain errors get durable rejection audit.
        record_rejection(  # Record failed posting attempt.
            entity=entry.entity,  # Entity being posted into.
            action=FinanceAuditAction.JOURNAL_POST_REJECTED,  # Audit action for rejected journal post.
            exc=exc, actor_user=actor_user, target=entry,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Transactional journal posting implementation.
def _post_journal_atomic(
    entry,
    *,
    actor_user=None,
    allow_restricted: bool = False,
    allow_closed: bool = False,
    allow_control_accounts: bool = False,
):
    """The posting work proper, all in one transaction.

    Steps:
      1. Guard the period is open (SOFT_CLOSED only when ``allow_restricted``).
      2. Guard the lines balance (Σdebits == Σcredits, exact kobo).
      3. Hold shared locks on every line account in primary-key order, then guard
         its current active/postable state and any hand-typed control account.
      4. Apply the line amounts to the per-period :class:`AccountBalance` aggregates.
      5. Stamp the entry POSTED with ``posted_at``/``posted_by``.
      6. Write the authoritative ``JOURNAL_POSTED`` audit row - same commit as 4–5.
    """
    from .audit import record
    from .models import JournalEntry

    # Serialise concurrent posts of the *same* entry: take a row lock and re-read the
    # status under it before doing anything. Without this, two requests can both pass
    # the status guard on a stale in-memory copy and each apply the lines to
    # AccountBalance - double-counting the ledger. The loser blocks here, then sees
    # POSTED and is rejected. (Document numbering is already lock-safe; posting was not.)
    locked_status = (  # Re-read status while holding the journal row lock.
        JournalEntry.objects.select_for_update()
        .values_list("status", flat=True).get(pk=entry.pk)
    )
    if locked_status == DocumentStatus.POSTED:  # Posted journals must not be posted twice.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is already posted.",
        )
    if locked_status in (DocumentStatus.REVERSED, DocumentStatus.CANCELLED):  # Terminal journals cannot post.
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} is '{locked_status}' and cannot be posted.",
        )

    ensure_period_open(  # Guard period status; CLOSED bypass is reserved for formal year close.
        entry.period,
        allow_restricted=allow_restricted,
        allow_closed=allow_closed,
        branch=entry.branch_id,
    )

    lines = list(entry.lines.select_related("account").all())
    if not lines:  # A journal with no lines has no accounting substance.
        raise PostingError("A journal must have at least one line to post.")

    total_debit, total_credit = sum_sides(lines)  # Calculate exact debit and credit totals.
    ensure_balanced(total_debit, total_credit)  # Enforce double-entry equality.

    locked_accounts = _lock_accounts_for_posting(line.account_id for line in lines)
    for line in lines:  # Validate every account touched by the journal.
        account = locked_accounts[line.account_id]
        if not (account[1] and account[2]):  # Inactive/header accounts cannot post.
            raise InactiveAccountError(account_code=account[0])
    _ensure_counterparties(entry, lines)

    if not allow_control_accounts:
        from .control_accounts import HAND_SOURCES, ensure_no_control_lines

        if entry.source in HAND_SOURCES:
            ensure_no_control_lines(entry.entity, [line.account for line in lines])

    _apply_to_balances(entry, sign=+1)  # Add line amounts to per-period balances.

    entry.status = DocumentStatus.POSTED  # Mark journal posted.
    entry.posted_at = timezone.now()
    entry.posted_by = actor_user  # Store posting actor.
    entry.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])

    record(  # Audit the successful journal post.
        entity=entry.entity,  # Entity posted into.
        action=FinanceAuditAction.JOURNAL_POSTED,  # Audit action.
        actor_user=actor_user, target=entry,  # Actor and target context.
        message=f"Posted into {entry.period}.",  # Human-readable audit message.
        after={"status": DocumentStatus.POSTED, "posted_at": entry.posted_at.isoformat()},  # Post-state snapshot.
        debit=total_debit, credit=total_credit,  # Structured totals.
    )
    return entry  # Return posted journal.


def _lock_accounts_for_posting(account_ids):
    """Read current account state under compatible locks in deterministic order.

    PostgreSQL ``FOR SHARE`` lets unrelated journals and journals using the same
    account post concurrently, while it conflicts with the ``FOR UPDATE`` lock
    used by account lifecycle operations. A cutover can therefore validate and
    retire a ledger without a posting slipping between those two steps.
    """
    from django.db import connections, router

    from .models import Account

    ids = sorted(set(account_ids))
    connection = connections[router.db_for_write(Account)]
    quote = connection.ops.quote_name
    placeholders = ", ".join(["%s"] * len(ids))
    lock = " FOR SHARE" if connection.vendor == "postgresql" else ""
    table = quote(Account._meta.db_table)
    pk = quote(Account._meta.pk.column)
    sql = (
        f"SELECT {pk}, {quote('code')}, {quote('is_active')}, {quote('is_postable')} "
        f"FROM {table} WHERE {pk} IN ({placeholders}) ORDER BY {pk}{lock}"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, ids)
        rows = cursor.fetchall()
    if len(rows) != len(ids):
        raise PostingError("A journal line names an account that no longer exists.")
    return {row[0]: row[1:] for row in rows}


def _ensure_counterparties(entry, lines) -> None:
    """Refuse an inter-branch line whose counterparty is its own branch or another tenant's.

    A line naming a counterparty branch says "this branch's balance with that
    one". Naming the entry's own branch would book a balance a branch owes
    itself, and naming another tenant's branch would carry a balance across
    tenants, so both are refused. Lines naming none, which is every other line,
    cost nothing here.
    """
    named = {line.counterparty_branch_id for line in lines if line.counterparty_branch_id}
    if not named:
        return
    from vs_tenants.models import Branch

    if entry.branch_id in named:
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} names its own branch as the "
            f"other branch of an inter-branch line.",
        )
    tenants = set(
        Branch.all_objects.filter(pk__in=named).values_list("tenant_id", flat=True)
    )
    if tenants != {entry.entity.tenant_id}:
        raise PostingError(
            f"Journal {entry.document_number or entry.pk} names a branch outside these "
            f"books as the other branch of an inter-branch line.",
        )


def _journal_document_owner(entry):
    """Return the sub-ledger document ``entry`` belongs to, or ``None``.

    A journal belongs to the first document whose journal field points at it. This
    deliberately uses model metadata rather than a short hard-coded list: new
    finance/procurement documents that add a ``journal``/``*_journal`` FK are guarded
    automatically instead of quietly reopening the same bypass.

    A reversal belongs to the document of the journal it reverses, followed down
    the ``reverses`` chain. Voiding a document posts a mirror journal linked to the
    original only through ``reverses``; were that mirror ownerless, a raw reverse of
    it would put the voided document's whole effect back into the ledger while the
    document still read voided. A manual journal has no owner, so neither does its
    reversal, and both stay reversible on their own.

    A year-end closing journal belongs to the fiscal year it closes
    (``closes_fiscal_year``), and so, through ``reverses``, does the reversal that
    reopening the year posts. Reversing either by hand would move a whole year's
    result in or out of Retained Earnings while the year's status said otherwise.

    A payroll run that pays several branches posts one journal per branch through
    a :class:`~vs_finance.models.PayrollRunBranch` share; those journals belong to
    the run, because cancelling the run is the only thing that may reverse them
    (each on its own branch). Reversing one share's journal by hand would put that
    branch's salary cost back while the run still read posted.

    An inter-branch transfer posts one journal per branch through its two
    :class:`~vs_finance.models.InterBranchTransferLeg` rows; both belong to the
    transfer, since reversing one side alone would leave one branch owed money
    the other no longer owes.

    A stock movement names the journal it was valued in, and so does the goods
    receipt or goods return that raised that journal. The document wins: a stock
    movement is the owner only of a journal no document claims (an issue or an
    adjustment), so a receipt's journal is never reported as belonging to one of
    its own stock lines.
    """
    from django.core.exceptions import ObjectDoesNotExist

    if entry.reverses_id is not None:
        return _journal_document_owner(entry.reverses)

    if getattr(entry, "closes_fiscal_year_id", None) is not None:
        return entry.closes_fiscal_year

    stock_movement = None
    for relation in entry._meta.related_objects:
        if "journal" not in relation.field.name:
            continue
        accessor = relation.get_accessor_name()
        try:
            related = getattr(entry, accessor)
            owner = related.order_by("pk").first() if relation.one_to_many else related
        except ObjectDoesNotExist:
            continue
        if owner is None:
            continue
        if type(owner).__name__ == "StockMovement":
            stock_movement = stock_movement or owner
            continue
        # A later credit/advance reclassification belongs to its receipt, note or
        # vendor payment, not to the small linkage row that makes the relationship
        # durable. Both sides of the ledger park early money in a holding account and
        # reclassify it when the document it belongs to arrives.
        if type(owner).__name__ == "CustomerCreditAllocationJournal":
            owner = owner.payment or owner.note
        elif type(owner).__name__ == "VendorAdvanceAllocationJournal":
            owner = owner.payment
        elif type(owner).__name__ == "VendorCreditAllocationJournal":
            owner = owner.note
        elif type(owner).__name__ == "PayrollRunBranch":
            owner = owner.run
        elif type(owner).__name__ == "InterBranchTransferLeg":
            owner = owner.transfer
        return owner
    if stock_movement is not None:
        return stock_movement

    # Pre-migration later-allocation journals were linked only through the
    # append-only audit metadata.  Keep the raw-reversal guard fail-closed even if a
    # historical row could not be backfilled into CustomerCreditAllocationJournal.
    from .constants import FinanceAuditAction, FinanceAuditStatus
    from .models import CreditNote, FinanceAuditLog, Payment

    legacy_sources = (
        (FinanceAuditAction.PAYMENT_ALLOCATED, Payment),
        (FinanceAuditAction.CREDIT_NOTE_ALLOCATED, CreditNote),
    )
    for action, model in legacy_sources:
        audit = FinanceAuditLog.objects.filter(
            entity=entry.entity, action=action, status=FinanceAuditStatus.SUCCESS,
            metadata__journal_id=entry.pk,
        ).order_by("pk").first()
        if audit and str(audit.target_id).isdigit():
            owner = model.objects.filter(pk=int(audit.target_id), entity=entry.entity).first()
            if owner is not None:
                return owner
    return None


#: Model name -> (the ``document_type`` the journal screen is given, the route under
#: ``/v1/`` that undoes the document). A payables document is undone by its own
#: service like a receivables one: a bill is voided, a vendor credit note voided, a
#: vendor payment reversed, and a goods receipt reversed by returning its goods.
_DOCUMENT_VOID_ROUTES = {
    "Invoice": ("INVOICE", "finance/invoices/{pk}/void/"),
    "Payment": ("PAYMENT", "finance/payments/{pk}/void/"),
    "CreditNote": ("CREDIT_NOTE", "finance/credit-notes/{pk}/void/"),
    "Refund": ("REFUND", "finance/refunds/{pk}/void/"),
    "Concession": ("CONCESSION", "finance/concessions/{pk}/void/"),
    "BankTransaction": ("BANK_TRANSACTION", "finance/bank-transactions/{pk}/void/"),
    "BankTransfer": ("BANK_TRANSFER", "finance/bank-transfers/{pk}/void/"),
    "InterBranchTransfer": ("INTER_BRANCH_TRANSFER", "finance/inter-branch-transfers/{pk}/void/"),
    "HeldForBranchReceipt": ("HELD_RECEIPT", "finance/held-receipts/{pk}/void/"),
    "PettyCashReturn": ("PETTY_CASH_RETURN", "finance/petty-cash-returns/{pk}/void/"),
    "VendorInvoice": ("VENDOR_INVOICE", "procurement/vendor-invoices/{pk}/void/"),
    "VendorCreditNote": ("VENDOR_CREDIT_NOTE", "procurement/vendor-credit-notes/{pk}/void/"),
    "VendorPayment": ("VENDOR_PAYMENT", "procurement/vendor-payments/{pk}/reverse/"),
    "GoodsReceivedNote": ("GOODS_RECEIVED_NOTE", "procurement/goods-receipts/{pk}/reverse/"),
}


def journal_reversal_action(entry):
    """Describe the safe action the journal screen may offer.

    The frontend must never infer document ownership from narration/source text.
    This contract is derived from the same relationship lookup that enforces the
    raw-reversal guard, so the button and the service cannot disagree.

    The reversal a void posted names its document as ``SOURCE_DOCUMENT_ACTION``
    rather than offering the void again: the document is already void, and the
    screen offers no button for that kind. A year-end closing journal, and the
    reversal that reopening its year posts, name their ``FiscalYear`` the same way.
    """
    owner = _journal_document_owner(entry)
    if owner is None:
        return {"kind": "REVERSE_JOURNAL"}

    model_name = type(owner).__name__
    config = _DOCUMENT_VOID_ROUTES.get(model_name)
    # A void's own reversal is undone on no screen: the document is already void.
    if config is None or entry.reverses_id is not None:
        return {
            "kind": "SOURCE_DOCUMENT_ACTION",
            "document_type": model_name,
            "document_number": _owner_label(owner),
        }

    document_type, _ = config
    return {
        "kind": "VOID_DOCUMENT",
        "document_type": document_type,
        "document_id": owner.pk,
        "document_number": _owner_label(owner),
    }


def _owner_label(owner):
    """The name a person knows ``owner`` by: its document number, or ``FY2026`` for a year."""
    if type(owner).__name__ == "FiscalYear":
        return f"FY{owner.year}"
    return getattr(owner, "document_number", "") or str(owner.pk)


def _document_void_instruction(owner):
    model_name = type(owner).__name__
    label = _owner_label(owner)
    config = _DOCUMENT_VOID_ROUTES.get(model_name)
    if config:
        _, route = config
        remedy = f"Use POST /{route.format(pk=owner.pk)} from the document screen instead."
    elif model_name == "PayrollRun":
        remedy = (
            f"Cancel payroll run {label} instead (POST /finance/payroll-runs/{owner.pk}/cancel/), "
            f"which reverses each branch's journal of the run."
        )
    elif model_name == "FiscalYear":
        remedy = (
            f"Reopen fiscal year {label} instead (POST /finance/fiscal-years/{owner.pk}/reopen/ "
            f"with a reason), which reverses its closing journals inside the year."
        )
    else:
        remedy = f"Use the {model_name} document-level void/reversal service instead."
    return model_name, label, remedy


@transaction.atomic
# Reverse a posted journal.
def reverse_journal(entry, *, actor_user=None, date=None, allow_restricted: bool = False,
                    allow_closed: bool = False, document_owner=None):
    """Reverse a posted journal by raising a mirror-image entry that nets it to zero.

    The original is left untouched on the record (marked REVERSED) and a new journal -
    debits and credits swapped - is created and posted, linked back via ``reverses``.
    This is the audit-correct way to undo: history is appended to, never edited.

    The reversing entry posts into ``date``'s period (defaults to the original's
    period). Returns the new reversing entry.

    ``allow_closed`` passes the year-end escape hatch through to the posting guard.
    Only :func:`vs_finance.close.reopen_fiscal_year` sets it, to reverse a closing
    journal on its own date in a month that is normally CLOSED by then.
    """
    from .models import JournalEntry, JournalLine

    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)

    owner = _journal_document_owner(entry)
    if owner is not None:
        owner_matches = (
            document_owner is not None
            and type(owner) is type(document_owner)
            and owner.pk == document_owner.pk
        )
        if not owner_matches:
            model_name, label, remedy = _document_void_instruction(owner)
            raise PostingError(
                f"Journal {entry.document_number or entry.pk} belongs to "
                f"{model_name} {label} and cannot be reversed on its own. {remedy}",
                owner_type=model_name, owner_id=str(owner.pk),
                owner_document_number=label,
            )

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
    # correction can still be booked into the current open period - the standard way
    # to reverse after a period closes.
    reversal_date = date or entry.date  # Prefer explicit reversal date, otherwise original date.
    from .chronology import ensure_on_or_after
    tenant = entry.entity.tenant
    remedy = f"Date the reversal {format_date(entry.date, tenant)} or later."
    ensure_on_or_after(
        subject=f"Reversal of journal {entry.document_number or entry.pk}",
        subject_date=reversal_date,
        source=f"journal {entry.document_number or entry.pk}",
        source_date=entry.date,
        remedy=remedy, tenant=tenant,
    )
    period = resolve_period(entry.entity, reversal_date)  # Resolve period for selected reversal date.
    if entry.period is not None and entry.period.is_closing and reversal_date == entry.date:
        period = entry.period  # A closing journal is undone in its own closing period.
    if date is None and not _period_accepts_posting(  # Original period may now be closed.
        period, allow_restricted=allow_restricted, allow_closed=allow_closed,
    ):
        reversal_date = branch_today(entry.entity.tenant, entry.branch_id)
        # Falling forward to today must not turn a future-dated source into a
        # backdated reversal.  Re-run chronology after changing the date; the
        # surrounding transaction leaves the source untouched if today is earlier.
        ensure_on_or_after(
            subject=f"Reversal of journal {entry.document_number or entry.pk}",
            subject_date=reversal_date,
            source=f"journal {entry.document_number or entry.pk}",
            source_date=entry.date,
            remedy=remedy, tenant=tenant,
        )
        period = resolve_period(entry.entity, reversal_date)  # Resolve today's period.

    reversal = JournalEntry.objects.create(
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
        JournalLine.objects.create(
            entry=reversal,  # Attach to reversal journal.
            account=line.account,  # Same account as original line.
            debit=line.credit,   # swap sides  # Original credit becomes reversal debit.
            credit=line.debit,  # Original debit becomes reversal credit.
            description=f"Reversal: {line.description}".strip(": "),  # Label reversal line.
            cost_center=line.cost_center,  # Preserve analytics.
            dimensions=line.dimensions,  # Preserve dimensions.
            counterparty_branch_id=line.counterparty_branch_id,  # Same inter-branch pair.
            line_no=line.line_no,  # Preserve line order.
        )

    post_journal(  # Post mirror journal.
        reversal, actor_user=actor_user, allow_restricted=allow_restricted,
        allow_closed=allow_closed,
    )

    entry.status = DocumentStatus.REVERSED  # Mark original as reversed.
    entry.save(update_fields=["status", "updated_at"])

    from .audit import record
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
                      actor_user=None, branch=None, opening=False):  # Create and post a raw direct journal entry.
    """Create a direct entry with :func:`create_direct_entry` and post it straight away.

    For callers that have already settled that the entry needs no approval. The
    direct-entry route decides that from the school's journal route first.
    """
    entry = create_direct_entry(
        entity, lines=lines, date=date, narration=narration, reference=reference,
        actor_user=actor_user, branch=branch, opening=opening,
    )
    post_journal(entry, actor_user=actor_user)  # Run normal posting guards and balance updates.
    entry.refresh_from_db()
    return entry  # Return posted direct entry.


@transaction.atomic
def create_direct_entry(entity, *, lines, date=None, narration="", reference="",
                        actor_user=None, branch=None, opening=False):  # Create a raw direct journal entry as a draft.
    """Build a direct journal entry - money/balances seated into the books with no source doc.

    This is the way to record a balance no sub-ledger document carries: accruals,
    reclassifications between ordinary accounts, depreciation adjustments and the like.
    Unlike sub-ledger postings (which derive their journal from an invoice/payment/etc.),
    a direct entry is the one place a caller supplies raw lines. It posts with
    ``source=MANUAL``, which is what it is: a journal a person typed. ``opening`` marks
    a true opening balance (brought forward on the day the books began, such as the
    cost of fixed assets already owned) and posts it with ``source=OPENING`` instead,
    which is also what keeps it from counting as the books' first trading
    (:func:`vs_finance.opening_balances.books_went_live` reads it that way).

    It may not name an account a sub-ledger keeps (:mod:`vs_finance.control_accounts`):
    money into or out of a bank account is a bank transaction, an opening customer
    balance an opening invoice, and an opening supplier balance an opening bill. The
    refusal happens here, before the draft exists, so a journal that could never post
    is not sent round an approval route first.

    ``lines`` is a list of ``(account, debit_kobo, credit_kobo)`` - optionally extended
    with a 4th element ``cost_center`` and a 5th element ``dimensions`` - where ``account``
    is a code string (resolved within ``entity``) or an :class:`~vs_finance.models.Account`,
    the optional ``cost_center`` is a :class:`~vs_finance.models.CostCenter` (or ``None``)
    and ``dimensions`` is a ``{axis_code: value}`` map, both carried onto the GL line. The
    entry must balance (Σdebits == Σcredits); it posts into
    ``date``'s open period - ``date`` defaults to the entity's earliest period start, else
    today. The entry is returned as a DRAFT: the normal :func:`post_journal` guards
    (period open, balanced, accounts active/postable) apply when it is posted, directly
    or on approval, and it is reversible like any journal.

    ``branch`` is the branch the entry belongs to, or ``None`` for an entry shared
    across the school. A direct entry starts a chain, so an API caller settles it
    with the platform rule for that (:func:`vs_rbac.scoping.raised_branch`) before
    calling here; this function records it and does not judge it.
    """
    from .accounts import resolve_account
    from .control_accounts import ensure_no_control_lines
    from .models import FiscalPeriod, JournalEntry, JournalLine

    rows = list(lines or [])  # Normalize iterable input and handle None.
    if not rows:  # Direct entries must include at least one line.
        raise PostingError("A direct entry needs at least one line.")
    ensure_no_control_lines(entity, [
        row[0] if not isinstance(row[0], str) else resolve_account(entity, row[0])
        for row in rows
    ])

    if date is None:  # Default direct-entry date when caller omits one.
        date = (  # Prefer earliest fiscal period start, otherwise today.
            FiscalPeriod.objects.filter(entity=entity)
            .order_by("start_date").values_list("start_date", flat=True).first()
            or branch_today(entity.tenant, branch)
        )

    entry = JournalEntry.objects.create(
        entity=entity, branch=branch,  # Entity and the branch the entry was raised for.
        date=date, period=resolve_period(entity, date),  # Date and resolved period.
        source=JournalSource.OPENING if opening else JournalSource.MANUAL,
        narration=narration or ("Opening balances" if opening else "Direct entry"),
        reference=reference, created_by=actor_user,  # External reference and actor.
    )
    for i, row in enumerate(rows, start=1):  # Create journal lines in input order.
        # optional 4th element: cost_center, optional 5th: dimensions JSON map  # Extra tuple values carry analytics.
        account, debit, credit, *rest = row  # Unpack mandatory and optional line values.
        cost_center = rest[0] if rest else None  # Optional cost center.
        dimensions = rest[1] if len(rest) > 1 else {}  # Optional dimensions JSON map.
        acct = account if not isinstance(account, str) else resolve_account(entity, account)  # Resolve code strings.
        JournalLine.objects.create(
            entry=entry, account=acct,  # Attach line to entry and account.
            debit=int(debit or 0), credit=int(credit or 0),  # Store integer kobo side amounts.
            cost_center=cost_center, dimensions=dimensions or {}, line_no=i,  # Store analytics and line number.
        )
    return entry  # Return the draft; posting or submitting it is the caller's decision.
