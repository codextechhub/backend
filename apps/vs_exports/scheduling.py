"""Occurrence maths for export schedules.

The rule that shapes this module: **store local time plus a timezone name, never a
UTC offset.** Everything here works in the schedule's own zone and converts to UTC
only at the last step, which is why a 03:00 run stays at 03:00 on both sides of a
clock change. ``next_run_at`` is a derived index for the dispatcher, not the source
of truth - it is always recomputed from the local fields.

The second rule: a window missed through an outage runs once on recovery if we are
inside :data:`~vs_exports.constants.MISSED_WINDOW_GRACE_HOURS`, and is otherwise
skipped and reported. Catching up six stale nightly files helps nobody.
"""
from __future__ import annotations

import calendar
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from .constants import MISSED_WINDOW_GRACE_HOURS, Recurrence, ScheduleState

#: Used when a schedule names a zone this machine does not know. Lagos is the
#: platform's home zone, and a schedule that silently stopped firing would be worse
#: than one that fires an hour off.
DEFAULT_TIMEZONE = "Africa/Lagos"


# Resolve a schedule's timezone, falling back to the platform default.
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


# Combine a local date and time into an aware datetime in the schedule's zone.
def _local(date: datetime.date, at_time: datetime.time, zone: ZoneInfo) -> datetime.datetime:
    """Fold the wall-clock time onto ``date`` in ``zone``.

    ``fold=0`` resolves the ambiguous hour when clocks go back to its first
    occurrence, which is the conventional reading of "03:00 every day".
    """
    return datetime.datetime.combine(date, at_time).replace(tzinfo=zone, fold=0)


# Clamp a day-of-month to the length of that month.
def _clamp_day(year: int, month: int, day: int) -> datetime.date:
    """The 31st of a 30-day month means its last day, not a skipped occurrence."""
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(day, last))


# Advance a month number by ``months``, returning (year, month).
def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + months
    return index // 12, index % 12 + 1


def next_occurrence(schedule, *, after=None) -> datetime.datetime | None:
    """First run time strictly after ``after`` (default: now), as an aware UTC datetime.

    Returns ``None`` when the schedule has no future occurrence - a ONCE schedule that
    has already fired, or a recurring one past its end date. Callers treat that as
    *finished*, never as *paused*.
    """
    after = after or timezone.now()
    zone = _zone(schedule.timezone_name)
    after_local = after.astimezone(zone)

    starts_on = schedule.starts_on
    ends_on = schedule.ends_on

    # Never schedule before the start date, whatever ``after`` says.
    cursor = max(after_local.date(), starts_on)

    def _emit(date: datetime.date):
        """The UTC instant for ``date``, if it is in range and still ahead."""
        if ends_on and date > ends_on:
            return None
        moment = _local(date, schedule.at_time, zone)
        if moment <= after_local:
            return None
        return moment.astimezone(datetime.timezone.utc)

    if schedule.recurrence == Recurrence.ONCE:
        return _emit(starts_on)

    if schedule.recurrence == Recurrence.DAILY:
        # Today if its time has not passed, otherwise tomorrow.
        return _emit(cursor) or _emit(cursor + datetime.timedelta(days=1))

    if schedule.recurrence == Recurrence.WEEKLY:
        target = schedule.day if schedule.day is not None else starts_on.weekday()
        target = int(target) % 7
        for step in range(0, 15):
            date = cursor + datetime.timedelta(days=step)
            if date.weekday() != target:
                continue
            moment = _emit(date)
            if moment:
                return moment
            if ends_on and date > ends_on:
                return None
        return None

    if schedule.recurrence in (Recurrence.MONTHLY, Recurrence.QUARTERLY):
        stride = 1 if schedule.recurrence == Recurrence.MONTHLY else 3
        day = int(schedule.day or starts_on.day)
        year, month = cursor.year, cursor.month
        # Quarterly keeps the start month's phase (Jan/Apr/Jul/Oct for a Jan start).
        if stride == 3:
            drift = (month - starts_on.month) % 3
            if drift:
                year, month = _add_months(year, month, 3 - drift)
        for _ in range(0, 13):
            candidate = _clamp_day(year, month, day)
            if candidate >= cursor:
                moment = _emit(candidate)
                if moment:
                    return moment
                if ends_on and candidate > ends_on:
                    return None
            year, month = _add_months(year, month, stride)
        return None

    return None


# Decide whether a missed window should still run.
def should_run_missed(due_at, *, now=None) -> bool:
    """A window missed by an outage runs once on recovery, inside the grace period."""
    now = now or timezone.now()
    if due_at is None:
        return False
    return (now - due_at) <= datetime.timedelta(hours=MISSED_WINDOW_GRACE_HOURS)


def describe(schedule) -> str:
    """The recurrence in the words the editor reads back to the person setting it.

    The UI shows this verbatim, so a schedule can be checked without anyone having to
    interpret a cron expression.
    """
    at = schedule.at_time.strftime("%H:%M")
    zone = schedule.timezone_name or DEFAULT_TIMEZONE
    day = schedule.day

    if schedule.recurrence == Recurrence.ONCE:
        body = f"runs once on {schedule.starts_on:%d %b %Y} at {at}"
    elif schedule.recurrence == Recurrence.DAILY:
        body = f"runs every day at {at}"
    elif schedule.recurrence == Recurrence.WEEKLY:
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
        weekday = names[int(day) % 7] if day is not None else names[schedule.starts_on.weekday()]
        body = f"runs every {weekday} at {at}"
    elif schedule.recurrence == Recurrence.MONTHLY:
        body = f"runs on day {day or schedule.starts_on.day} of every month at {at}"
    elif schedule.recurrence == Recurrence.QUARTERLY:
        body = f"runs on day {day or schedule.starts_on.day} of every third month at {at}"
    else:
        body = f"runs at {at}"

    tail = f" ({zone}), starting {schedule.starts_on:%d %b %Y}"
    tail += (
        f", ending {schedule.ends_on:%d %b %Y}" if schedule.ends_on
        else ", with no end date"
    )
    suffix = ". A clock change keeps the local time fixed."
    if schedule.state == ScheduleState.PAUSED:
        suffix += f" Currently paused: {schedule.pause_detail or 'no reason recorded'}."
    elif schedule.state == ScheduleState.FINISHED:
        suffix += " This schedule has finished."
    return body + tail + suffix
