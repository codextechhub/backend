"""Reading records as they stood at the end of a chosen day.

A page asks for a date with ``?as_at=YYYY-MM-DD``. :func:`parse_as_at` turns it
into an :class:`AsAt`, whose ``moment`` is the first instant of the following
day in :data:`RECORD_DAY_TIMEZONE`: a version counts when it was recorded
before that instant, so a change made at 21:00 on the chosen day is included
and one made at 00:30 the next morning is not.

Today, or no date at all, is the live record and answers ``None``, so a page
passing today's date behaves exactly as one passing nothing.

A date before a record's history starts is refused with
:class:`HistoryNotKept` (409), carrying the first date that can be answered.
The refusal is the point: showing today's values under an earlier date would
tell an auditor something the platform never recorded.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.db.models import Min
from django.utils import timezone

from .models import RecordVersion
from .registry import TrackedModel, owner_key, rebuild

#: The zone a school's day is counted in. The platform's schools keep West
#: Africa Time, the same default the Export Centre schedules against.
RECORD_DAY_TIMEZONE = ZoneInfo("Africa/Lagos")


class AsAtError(Exception):
    """A refusal about the date asked for, rendered by the core handler."""

    error_code = "AS_AT_INVALID"
    http_status = 400

    def __init__(self, message: str, **extra):
        self.message = message
        self.extra = extra
        super().__init__(message)


class HistoryNotKept(AsAtError):
    """The date asked for is earlier than anything recorded about the record."""

    error_code = "HISTORY_NOT_KEPT"
    http_status = 409


def record_today() -> dt.date:
    return timezone.now().astimezone(RECORD_DAY_TIMEZONE).date()


def record_date(moment: dt.datetime) -> dt.date:
    """The school day *moment* falls on."""
    return moment.astimezone(RECORD_DAY_TIMEZONE).date()


@dataclass(frozen=True)
class AsAt:
    """A past school day, and the instant its records are read at."""

    date: dt.date

    @property
    def moment(self) -> dt.datetime:
        """The first instant of the following day; versions before it count."""
        start = dt.datetime.combine(self.date + dt.timedelta(days=1), dt.time.min)
        return start.replace(tzinfo=RECORD_DAY_TIMEZONE)

    def includes(self, when) -> bool:
        """Whether *when* (a datetime or a date) had happened by the end of the day."""
        if when is None:
            return False
        if isinstance(when, dt.datetime):
            return when < self.moment
        return when <= self.date


def parse_as_at(request) -> AsAt | None:
    """The day ``?as_at=`` asks for, or ``None`` for the live record."""
    raw = (request.query_params.get("as_at") or "").strip()
    if not raw:
        return None
    try:
        day = dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise AsAtError(
            "Give the date as YYYY-MM-DD, for example 2026-03-05.", as_at=raw,
        ) from exc
    today = record_today()
    if day > today:
        raise AsAtError(
            "That date has not happened yet. Pick today or an earlier day.",
            as_at=raw,
        )
    if day == today:
        return None
    return AsAt(day)


def history_starts(spec: TrackedModel, record_id) -> dt.date | None:
    """The first school day this record can be read as at, or ``None``."""
    first = RecordVersion.objects.filter(
        record_type=spec.record_type, record_id=str(record_id),
    ).aggregate(first=Min("recorded_at"))["first"]
    return record_date(first) if first else None


def require_history(spec: TrackedModel, record_id, as_at: AsAt, *, noun: str) -> dt.date:
    """Raise :class:`HistoryNotKept` unless *as_at* is within the record's history.

    *noun* names the record in the refusal ("this student's record"). Returns
    the day the history starts.
    """
    starts = history_starts(spec, record_id)
    if starts is None or as_at.date < starts:
        when = f"{starts.day} {starts:%B %Y}" if starts else "today"
        raise HistoryNotKept(
            f"History for {noun} starts on {when}. Pick that day or a later one.",
            history_starts=starts.isoformat() if starts else None,
        )
    return starts


def version_at(spec: TrackedModel, record_id, as_at: AsAt) -> RecordVersion | None:
    """The version of one row in force at *as_at*, or ``None`` if it did not exist."""
    version = (
        RecordVersion.objects.filter(
            record_type=spec.record_type, record_id=str(record_id),
            recorded_at__lt=as_at.moment,
        )
        .order_by("-recorded_at", "-id")
        .first()
    )
    if version is None or version.is_deleted:
        return None
    return version


def instance_at(spec: TrackedModel, record_id, as_at: AsAt):
    """The row as it stood at *as_at*, rebuilt, or ``None`` if it did not exist."""
    version = version_at(spec, record_id, as_at)
    if version is None:
        return None
    instance = rebuild(spec, record_id, version.data)
    instance._history_version = version
    return instance


def instances_at(spec: TrackedModel, owner_type: str, owner_id, as_at: AsAt) -> list:
    """Every row of *spec* listed on one owner's page at *as_at*, rebuilt.

    One query: the latest version per row recorded before the moment, keeping
    only rows whose latest version is not a deletion. Ordered by primary key,
    so a caller sorting by something else starts from a stable order.
    """
    latest = (
        RecordVersion.objects.filter(
            record_type=spec.record_type,
            owners__contains=[owner_key(owner_type, owner_id)],
            recorded_at__lt=as_at.moment,
        )
        .order_by("record_id", "-recorded_at", "-id")
        .distinct("record_id")
    )
    rows = []
    for version in latest:
        if version.is_deleted:
            continue
        instance = rebuild(spec, version.record_id, version.data)
        instance._history_version = version
        rows.append(instance)
    pk_field = spec.model._meta.pk
    rows.sort(key=lambda row: pk_field.value_from_object(row))
    return rows


def instances_by_id_at(spec: TrackedModel, record_ids, as_at: AsAt) -> dict:
    """Rows of *spec* named by id, each as it stood at *as_at*; absent ones omitted."""
    ids = [str(pk) for pk in record_ids]
    if not ids:
        return {}
    latest = (
        RecordVersion.objects.filter(
            record_type=spec.record_type, record_id__in=ids,
            recorded_at__lt=as_at.moment,
        )
        .order_by("record_id", "-recorded_at", "-id")
        .distinct("record_id")
    )
    out = {}
    for version in latest:
        if version.is_deleted:
            continue
        instance = rebuild(spec, version.record_id, version.data)
        instance._history_version = version
        out[instance.pk] = instance
    return out
