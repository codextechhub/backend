"""Reading records as they stood at the end of a chosen day.

A page asks for a date with ``?as_at=YYYY-MM-DD``. :func:`parse_as_at` turns it
into an :class:`AsAt`, which carries the day and the zone that day is counted
in. Its ``moment`` is the first instant of the following day in that zone: a
version counts when it was recorded before that instant, so a change made at
21:00 on the chosen day is included and one made at 00:30 the next morning is
not.

**Whose day.** A day is the tenant's calendar day, never the server's and never
a fixed zone's. :func:`request_zone` resolves the zone from ``request.tenant``
through :func:`vs_config.clock.tenant_zone` (the platform's zone when there is
no tenant), so a school in Nairobi asking for 14 March at 00:30 on 15 March is
answered on Nairobi's calendar: the day has ended, the date is not refused as
being in the future, and every version recorded before Nairobi's midnight is
included. Every function here that turns an instant into a day takes the zone
from the :class:`AsAt` it is given, or from an explicit ``zone`` argument where
there is no :class:`AsAt` (the live record naming its first day), so there is
no default a caller can fall back to by leaving it out.

Today, or no date at all, is the live record and answers ``None``, so a page
passing today's date behaves exactly as one passing nothing.

A date before a record's history starts is refused with
:class:`HistoryNotKept` (409), carrying the first date that can be answered.
The refusal is the point: showing today's values under an earlier date would
tell an auditor something the platform never recorded.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.db.models import Min
from django.utils import timezone

from vs_config.clock import tenant_zone
from vs_config.display import format_date

from .models import RecordVersion, TrackingStart
from .registry import TrackedModel, owner_key, rebuild


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


def request_zone(request) -> ZoneInfo:
    """The zone the tenant *request* speaks for counts its days in."""
    return tenant_zone(getattr(request, "tenant", None))


def record_today(zone: ZoneInfo) -> dt.date:
    """The calendar day it currently is in *zone*."""
    return timezone.now().astimezone(zone).date()


def record_date(moment: dt.datetime, zone: ZoneInfo) -> dt.date:
    """The day *moment* falls on in *zone*."""
    return moment.astimezone(zone).date()


@dataclass(frozen=True)
class AsAt:
    """A past day, the zone it is counted in, and the instant it ends.

    ``tenant`` is the tenant the request speaks for, whose date format a
    refusal writes its dates in (:mod:`vs_config.display`); ``None`` writes
    them in the platform's.
    """

    date: dt.date
    zone: ZoneInfo
    tenant: object = field(default=None, compare=False)

    @property
    def moment(self) -> dt.datetime:
        """The first instant of the following day; versions before it count."""
        start = dt.datetime.combine(self.date + dt.timedelta(days=1), dt.time.min)
        return start.replace(tzinfo=self.zone)

    def includes(self, when) -> bool:
        """Whether *when* (a datetime or a date) had happened by the end of the day."""
        if when is None:
            return False
        if isinstance(when, dt.datetime):
            return when < self.moment
        return when <= self.date


def parse_as_at(request) -> AsAt | None:
    """The day ``?as_at=`` asks for, or ``None`` for the live record.

    The day is the calendar day of the tenant the request speaks for, so
    "today" and "not yet happened" are judged on that tenant's clock.
    """
    raw = (request.query_params.get("as_at") or "").strip()
    if not raw:
        return None
    try:
        day = dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise AsAtError(
            "Give the date as YYYY-MM-DD, for example 2026-03-05.", as_at=raw,
        ) from exc
    zone = request_zone(request)
    today = record_today(zone)
    if day > today:
        raise AsAtError(
            "That date has not happened yet. Pick today or an earlier day.",
            as_at=raw,
        )
    if day == today:
        return None
    return AsAt(day, zone, getattr(request, "tenant", None))


def history_starts(spec: TrackedModel, record_id, zone: ZoneInfo) -> dt.date | None:
    """The first day, in *zone*, this record can be read as at, or ``None``."""
    first = RecordVersion.objects.filter(
        record_type=spec.record_type, record_id=str(record_id),
    ).aggregate(first=Min("recorded_at"))["first"]
    return record_date(first, zone) if first else None


def require_history(spec: TrackedModel, record_id, as_at: AsAt, *, noun: str) -> dt.date:
    """Raise :class:`HistoryNotKept` unless *as_at* is within the record's history.

    *noun* names the record in the refusal ("this student's record"). Returns
    the day the history starts, counted in the zone *as_at* was asked in.
    """
    starts = history_starts(spec, record_id, as_at.zone)
    if starts is None or as_at.date < starts:
        when = format_date(starts, as_at.tenant, month="long") if starts else "today"
        raise HistoryNotKept(
            f"History for {noun} starts on {when}. Pick that day or a later one.",
            history_starts=starts.isoformat() if starts else None,
        )
    return starts


def tracking_starts(spec: TrackedModel, zone: ZoneInfo) -> dt.date | None:
    """The first day, in *zone*, any list of *spec* can be read as at, or ``None``.

    The stamped :class:`TrackingStart` when there is one. Before the baseline
    command has stamped it, the model's earliest version stands in, which can
    only be later than the true start and so never answers a day too early.
    """
    started = (
        TrackingStart.objects.filter(record_type=spec.record_type)
        .values_list("started_at", flat=True).first()
    )
    if started is None:
        started = RecordVersion.objects.filter(
            record_type=spec.record_type,
        ).aggregate(first=Min("recorded_at"))["first"]
    return record_date(started, zone) if started else None


def require_list_history(spec: TrackedModel, owner_starts: dt.date, as_at: AsAt,
                         *, noun: str) -> dt.date:
    """Raise :class:`HistoryNotKept` unless a list of *spec* is known at *as_at*.

    *owner_starts* is the day the owning record's history starts (from
    :func:`require_history`); the answer is the later of that day and the day
    tracking reached *spec*. *noun* names the list in the refusal ("this
    person's field exceptions"). Returns the day the list's history starts.
    """
    tracked = tracking_starts(spec, as_at.zone)
    starts = max(owner_starts, tracked) if tracked else None
    if starts is None or as_at.date < starts:
        when = format_date(starts, as_at.tenant, month="long") if starts else "today"
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
