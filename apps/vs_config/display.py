"""How a tenant's screens show a date and a time.

Three settings decide it, all read here so every reader gets the same answer:

* ``display.date_format``: ``D_MMM_YYYY`` ("29 Sep 2026", the default),
  ``DD_MM_YYYY`` ("29/09/2026") or ``YYYY_MM_DD`` ("2026-09-29"). One for the
  whole school. There is deliberately no month-first format: a school that
  wrote 03/04/2026 would have parents reading two different days.
* ``display.clock``: ``H12`` ("8:00 am", the default) or ``H24`` ("08:00").
  One for the whole school.
* ``display.timezone``: the school's zone, and each branch's own where it keeps
  one (:mod:`vs_config.clock`).

:func:`display_preferences` is the block a signed-in client caches with its
session, so every screen formats a date the school's way and knows which day
it is at each branch without asking.

**What the server prints.** Everything the server writes for a person to read
(an invoice, a receipt, a PDF statement, an email, a notification, a file an
export produces, a refusal sentence) writes its dates and times through the
functions below, so a document reads exactly as the school's screens do:

* :func:`format_date` - "29 Sep 2026", "29/09/2026" or "2026-09-29";
* :func:`format_datetime` - "29 Sep 2026, 2:30 pm" or "29/09/2026, 14:30";
* :func:`format_time` - "2:30 pm" or "14:30";
* :func:`format_month` - "Sep 2026" in every date format, because a month on
  its own is a heading, and "09/2026" reads as a card expiry;
* :func:`format_date_range` - "27 - 31 Oct 2025" and the other shortenings
  the screens use.

The output matches ``school-fe`` ``src/lib/dates.ts`` character for character:
English month names, lowercase "am" and "pm", no leading zero on a 12-hour
hour, and ", " between a date and its time.

Two kinds of value, never confused:

* **A calendar date** (a ``date``, or a ``YYYY-MM-DD`` string): a due date, a
  birthday, an exam day. It is the same day everywhere and no zone touches it.
* **An instant** (a ``datetime``): when something was saved, sent or expires.
  It is read on the wall clock of the branch it belongs to when that branch
  keeps its own zone, else the school's (:mod:`vs_config.clock`). A naive
  ``datetime`` is taken as UTC, which is what the server stores
  (``USE_TZ = True``, ``TIME_ZONE = "UTC"``).

A wall time with no date (a ``time``, or ``HH:MM``) is already the school's
local time and is only reworded for the clock.

**Resolution** is the configuration engine's: the school's value, else the
platform's, else the definition's default. A stored value outside the choices
(written around the engine's validation) reads as the default. ``None`` and
the platform tenant read the platform's values, so an email to a platform
operator follows the platform's settings.

**Caching.** The date format and the clock cost two queries together, and
they are memoised on the tenant instance beside the zone the clock memoises,
shared with the request's own instance of the same tenant. A list printing a
date per row asks the configuration once. :func:`vs_config.clock.forget_tenant_zone`
drops this memo too, so a value just written is read back in the same request.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from .clock import (
    _STYLE_MEMO_ATTRIBUTE,
    _config_tenant,
    _memo_holders,
    branch_zone,
    branch_zones,
    tenant_zone,
)


DATE_FORMAT_KEY = "display.date_format"
CLOCK_KEY = "display.clock"

D_MMM_YYYY = "D_MMM_YYYY"
DD_MM_YYYY = "DD_MM_YYYY"
YYYY_MM_DD = "YYYY_MM_DD"

#: Every date format a school may choose, in the order a screen offers them.
DATE_FORMATS = (D_MMM_YYYY, DD_MM_YYYY, YYYY_MM_DD)
DEFAULT_DATE_FORMAT = D_MMM_YYYY

H12 = "H12"
H24 = "H24"

#: Every clock a school may choose, in the order a screen offers them.
CLOCKS = (H12, H24)
DEFAULT_CLOCK = H12

_CHOICES = {
    DATE_FORMAT_KEY: (DATE_FORMATS, DEFAULT_DATE_FORMAT),
    CLOCK_KEY: (CLOCKS, DEFAULT_CLOCK),
}

#: What each clock's option is called on the settings screen.
_CLOCK_LABELS = {
    H12: "12-hour (8:00 am, 2:30 pm)",
    H24: "24-hour (08:00, 14:30)",
}


_MONTHS_LONG = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTHS_SHORT = tuple(name[:3] for name in _MONTHS_LONG)
_WEEKDAYS_LONG = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
_WEEKDAYS_SHORT = tuple(name[:3] for name in _WEEKDAYS_LONG)


def write_date(
    day: datetime.date,
    date_format: str,
    *,
    year: bool = True,
    month: str = "short",
    weekday: str | None = None,
) -> str:
    """*day* written in *date_format*, as the school's screens write it.

    The pure form, for a caller that already holds the format:
    :func:`format_date` reads the tenant's and calls this. ``year=False``
    drops the year ("29 Sep", "29/09", "09-29"); ``month="long"`` spells the
    month out in the D MMM YYYY style only; ``weekday`` ``"short"`` or
    ``"long"`` prefixes one ("Tuesday, 29 Sep 2026"). Month and weekday names
    are English whatever the server's locale, as they are on screen.
    """
    if date_format == DD_MM_YYYY:
        text = f"{day:%d/%m/%Y}" if year else f"{day:%d/%m}"
    elif date_format == YYYY_MM_DD:
        text = day.isoformat() if year else f"{day:%m-%d}"
    else:
        name = (_MONTHS_LONG if month == "long" else _MONTHS_SHORT)[day.month - 1]
        text = f"{day.day} {name} {day.year}" if year else f"{day.day} {name}"
    if not weekday:
        return text
    names = _WEEKDAYS_LONG if weekday == "long" else _WEEKDAYS_SHORT
    return f"{names[day.weekday()]}, {text}"


def write_time(hour: int, minute: int, clock: str) -> str:
    """A wall-clock reading on *clock*: "8:00 am", "2:30 pm" or "08:00", "14:30"."""
    if clock == H24:
        return f"{hour:02d}:{minute:02d}"
    return f"{hour % 12 or 12}:{minute:02d} {'pm' if hour >= 12 else 'am'}"


def date_format_options(example: datetime.date) -> list[dict]:
    """``[{value, label}]`` for the settings screen, each label *example* in it.

    The school's own today is the natural example, so an admin choosing sees
    the date they are looking at written each way.
    """
    return [
        {"value": value, "label": write_date(example, value)}
        for value in DATE_FORMATS
    ]


def clock_options() -> list[dict]:
    """``[{value, label}]`` for the settings screen."""
    return [{"value": value, "label": _CLOCK_LABELS[value]} for value in CLOCKS]


def resolve_display_choices(tenant) -> dict:
    """``{key: (value, source)}`` for the date format and the clock.

    ``source`` is ``school``, ``platform`` or ``default``, as the settings
    screen reports it. Two queries for both settings, whatever the tenant.
    """
    from .models import ConfigurationDefinition, ConfigurationValue

    tenant = _config_tenant(tenant)
    definitions = {
        row.key: row
        for row in ConfigurationDefinition.objects.filter(
            key__in=list(_CHOICES), is_active=True,
        )
    }
    scopes = ["platform"] + ([f"tenant:{tenant.pk}"] if tenant is not None else [])
    stored = {
        (row.definition_id, row.scope_key): row.value
        for row in ConfigurationValue.all_objects.filter(
            definition__in=list(definitions.values()), scope_key__in=scopes,
        )
    }
    resolved = {}
    for key, (choices, default) in _CHOICES.items():
        definition = definitions.get(key)
        value, source = default, "default"
        if definition is not None:
            for scope in reversed(scopes):
                if (definition.id, scope) in stored:
                    value = stored[(definition.id, scope)]
                    source = "platform" if scope == "platform" else "school"
                    break
        if value not in choices:
            value, source = default, "default"
        resolved[key] = (value, source)
    return resolved


def display_preferences(tenant) -> dict:
    """The block a client caches for the session: how to show dates and times.

    ::

        {"time_zone": "Africa/Lagos", "date_format": "D_MMM_YYYY",
         "clock": "H12", "branch_zones": {"12": "Africa/Nairobi"}}

    ``branch_zones`` names only the branches that keep a zone of their own,
    keyed by branch id; every other branch keeps ``time_zone``. It is the whole
    school's, not narrowed to the reader: a zone is no secret, and a client
    showing a shared record from another branch needs that branch's day. The
    platform tenant, and ``None``, answer the platform's values and no branch
    zones.
    """
    tenant = _config_tenant(tenant)
    choices = resolve_display_choices(tenant)
    return {
        "time_zone": tenant_zone(tenant).key,
        "date_format": choices[DATE_FORMAT_KEY][0],
        "clock": choices[CLOCK_KEY][0],
        "branch_zones": {
            str(branch_id): zone.key
            for branch_id, zone in sorted(branch_zones(tenant).items())
        },
    }


# -- Printing for people ----------------------------------------------------- #

@dataclass(frozen=True)
class DisplayStyle:
    """How one tenant writes a date and a time."""

    date_format: str = DEFAULT_DATE_FORMAT
    clock: str = DEFAULT_CLOCK


def display_style(tenant) -> DisplayStyle:
    """*tenant*'s date format and clock, memoised on the tenant instance.

    ``None`` and the platform tenant read the platform's values and are not
    memoised: there is no instance to hang a memo on, and a module-level cache
    would outlive the request in a long-lived worker.
    """
    tenant = _config_tenant(tenant)
    if tenant is None:
        return _read_style(None)
    holders = _memo_holders(tenant)
    for holder in holders:
        style = getattr(holder, _STYLE_MEMO_ATTRIBUTE, None)
        if style is not None:
            break
    else:
        style = _read_style(tenant)
    for holder in holders:
        setattr(holder, _STYLE_MEMO_ATTRIBUTE, style)
    return style


def _read_style(tenant) -> DisplayStyle:
    choices = resolve_display_choices(tenant)
    return DisplayStyle(
        date_format=choices[DATE_FORMAT_KEY][0], clock=choices[CLOCK_KEY][0],
    )


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_WALL_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$")


def _as_day(value):
    """*value* as a calendar ``date`` when it is one (never an instant), else ``None``."""
    if isinstance(value, datetime.datetime):
        return None
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and _ISO_DAY.match(value.strip()):
        try:
            return datetime.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _as_instant(value):
    """*value* as an aware ``datetime``, a naive one taken as UTC; else ``None``."""
    if isinstance(value, str):
        text = value.strip()
        if not re.search(r"\d[T ]\d", text):
            return None
        try:
            value = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _local(instant, tenant, branch):
    """*instant* on the wall clock of *branch* (its own zone), else of *tenant*."""
    return instant.astimezone(branch_zone(tenant, branch))


def format_date(
    value,
    tenant,
    *,
    branch=None,
    year: bool = True,
    month: str = "short",
    weekday: str | None = None,
) -> str:
    """The date of *value* in *tenant*'s date format.

    A calendar date (a ``date`` or ``YYYY-MM-DD``) is printed as it is, never
    shifted. An instant is first dated at *branch* (its own zone where it keeps
    one, else the school's), so a payment saved at 23:30 UTC is dated the next
    morning in Lagos. ``None`` and ``""`` print as ``""``; anything unreadable
    comes back as written, so a bad value is visible rather than blank. See
    :func:`write_date` for ``year``, ``month`` and ``weekday``.
    """
    if value is None or value == "":
        return ""
    day = _as_day(value)
    if day is None:
        instant = _as_instant(value)
        if instant is None:
            return str(value)
        day = _local(instant, tenant, branch).date()
    return write_date(
        day, display_style(tenant).date_format, year=year, month=month, weekday=weekday,
    )


def format_datetime(
    value,
    tenant,
    *,
    branch=None,
    with_zone: bool = False,
    weekday: str | None = None,
) -> str:
    """An instant as date and time: "29 Sep 2026, 2:30 pm" or "29/09/2026, 14:30".

    Read on *branch*'s wall clock where it keeps its own zone, else *tenant*'s.
    ``with_zone`` appends the zone's abbreviation ("29 Sep 2026, 2:30 pm WAT"),
    for a reader outside the school (a vendor, a person resetting a password)
    who cannot be assumed to share its clock. A calendar date has no time and
    prints as :func:`format_date` does, so a value that is sometimes one and
    sometimes the other never gains a made-up midnight.
    """
    if value is None or value == "":
        return ""
    if _as_day(value) is not None:
        return format_date(value, tenant, weekday=weekday)
    instant = _as_instant(value)
    if instant is None:
        return str(value)
    local = _local(instant, tenant, branch)
    style = display_style(tenant)
    text = (
        f"{write_date(local.date(), style.date_format, weekday=weekday)}, "
        f"{write_time(local.hour, local.minute, style.clock)}"
    )
    return f"{text} {local.tzname()}" if with_zone else text


def format_time(value, tenant, *, branch=None, with_zone: bool = False) -> str:
    """A time on *tenant*'s clock: "8:00 am" or "08:00".

    A wall time (a ``time``, or ``HH:MM`` or ``HH:MM:SS``) is already local and
    is only reworded. An instant is read on *branch*'s wall clock, else the
    school's, with its zone's abbreviation when ``with_zone`` is set. Seconds
    are dropped: nothing a school reads is timed to the second.
    """
    if value is None or value == "":
        return ""
    clock = display_style(tenant).clock
    if isinstance(value, datetime.time):
        return write_time(value.hour, value.minute, clock)
    if isinstance(value, str):
        match = _WALL_TIME.match(value.strip())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour <= 23 and minute <= 59:
                return write_time(hour, minute, clock)
            return value
    instant = _as_instant(value)
    if instant is None:
        return str(value)
    local = _local(instant, tenant, branch)
    text = write_time(local.hour, local.minute, clock)
    return f"{text} {local.tzname()}" if with_zone else text


def format_month(value, tenant, *, branch=None, month: str = "short") -> str:
    """A month and year as a label: "Sep 2026", or "September 2026" with ``month="long"``.

    Spelled with the month's name in every date format: a month on its own is
    a heading or a period, which a reader scans rather than matches against a
    record, and "09/2026" reads as a card expiry. *value* is a ``date``, a
    ``(year, month)`` pair, ``YYYY-MM`` or ``YYYY-MM-DD``, or an instant, which
    is placed on *branch*'s clock (else the school's) first. *tenant* matters
    only for an instant.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, tuple) and len(value) == 2:
        year, number = int(value[0]), int(value[1])
    elif isinstance(value, str) and _ISO_MONTH.match(value.strip()):
        year, number = (int(part) for part in _ISO_MONTH.match(value.strip()).groups())
    else:
        day = _as_day(value)
        if day is None:
            instant = _as_instant(value)
            if instant is None:
                return str(value)
            day = _local(instant, tenant, branch).date()
        year, number = day.year, day.month
    if not 1 <= number <= 12:
        return str(value)
    names = _MONTHS_LONG if month == "long" else _MONTHS_SHORT
    return f"{names[number - 1]} {year}"


def format_date_range(start, end, tenant, *, branch=None) -> str:
    """A span of days, as short as it can be said without losing a part.

    In the D MMM YYYY format: "21 Nov 2025", "27 - 31 Oct 2025", "28 Oct -
    2 Nov 2025" or "19 Dec 2025 - 2 Jan 2026". The numeric formats always print
    both dates in full ("27/10/2025 - 31/10/2025"), because half a numeric date
    is a puzzle. A one-day span, or one with no end, reads as one date.
    """
    if start is None or start == "":
        return ""
    if end is None or end == "" or end == start:
        return format_date(start, tenant, branch=branch)
    a = _as_day(start) or _instant_day(start, tenant, branch)
    b = _as_day(end) or _instant_day(end, tenant, branch)
    date_format = display_style(tenant).date_format
    if a is None or b is None:
        return f"{format_date(start, tenant, branch=branch)} - {format_date(end, tenant, branch=branch)}"
    if a == b:
        return write_date(a, date_format)
    if date_format != D_MMM_YYYY or a.year != b.year:
        return f"{write_date(a, date_format)} - {write_date(b, date_format)}"
    if a.month != b.month:
        return f"{write_date(a, date_format, year=False)} - {write_date(b, date_format)}"
    return f"{a.day} - {write_date(b, date_format)}"


def _instant_day(value, tenant, branch):
    instant = _as_instant(value)
    return None if instant is None else _local(instant, tenant, branch).date()
