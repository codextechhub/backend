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
it is at each branch without asking. Server-rendered documents (invoices,
receipts, PDFs, emails, exports) do not read these yet.

Resolution is the configuration engine's: the school's value, else the
platform's, else the definition's default. A stored value outside the choices
(written around the engine's validation) reads as the default.
"""
from __future__ import annotations

import datetime

from .clock import _config_tenant, branch_zones, tenant_zone


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


def format_date(day: datetime.date, date_format: str) -> str:
    """*day* written in *date_format*, as the school's screens write it."""
    if date_format == DD_MM_YYYY:
        return f"{day:%d/%m/%Y}"
    if date_format == YYYY_MM_DD:
        return day.isoformat()
    return f"{day.day} {day:%b %Y}"


def date_format_options(example: datetime.date) -> list[dict]:
    """``[{value, label}]`` for the settings screen, each label *example* in it.

    The school's own today is the natural example, so an admin choosing sees
    the date they are looking at written each way.
    """
    return [
        {"value": value, "label": format_date(example, value)}
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
