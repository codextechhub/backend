"""A tenant's own clock: its time zone, its "now" and its "today".

The server runs in UTC (``settings.TIME_ZONE``), so ``timezone.localdate()``
and ``date.today()`` both answer with the UTC calendar day. For a tenant in
Lagos that day is wrong for the first hour after every midnight: at 00:30 in
Lagos the server still thinks it is yesterday, so an invoice raised then is
dated yesterday and a deadline that fell due at midnight is not yet overdue.

Each tenant therefore keeps its own zone in the ``display.timezone``
configuration value, and anything meaning "the tenant's calendar day" or "the
tenant's wall clock" asks this module rather than the server:

* :func:`tenant_zone` - the tenant's ``ZoneInfo``;
* :func:`tenant_now` - an aware ``datetime`` in that zone;
* :func:`tenant_today` - the calendar ``date`` in that zone.

A stored instant stays ``timezone.now()``. Only a *day* or a *wall-clock
reading* comes from here.

**Resolution.** The tenant's own value, then the platform value, then
:data:`DEFAULT_TIME_ZONE` (Africa/Lagos), which is also the definition's
default. ``None`` and the platform tenant itself both read the platform layer,
so a caller with no tenant in reach still gets the platform's day rather than
UTC's.

**Why here.** The value is a ``vs_config`` definition read through
:func:`vs_config.conf.get_config`, and ``vs_config`` depends on no domain app,
so every engine and every school app can import this without a cycle.
``core`` would need to reach up into ``vs_config`` to answer, which is the
wrong direction for the lowest layer.

**Caching.** ``get_config`` is not cached and costs two queries, so the zone is
memoised on the tenant instance. ``request.tenant`` lives for one request and a
task loads its own, so the memo never outlives the unit of work that read it.
Another instance of the request's own tenant (a row's ``row.tenant``, say)
shares the memo held on the request's instance, so a list reading "today" per
row asks the configuration once, not once per row. A write through the display
settings endpoint calls :func:`forget_tenant_zone` so the refreshed answer in
the same request is the new value. The platform layer (``tenant=None``) is not
memoised: there is no instance to hang it on, and a module-level cache would
survive into the next request in a long-lived worker.
"""
from __future__ import annotations

import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.utils import timezone


#: The configuration key holding a tenant's IANA time zone name.
TIME_ZONE_KEY = "display.timezone"

#: Every tenant starts here until it chooses otherwise.
DEFAULT_TIME_ZONE = "Africa/Lagos"

_MEMO_ATTRIBUTE = "_vs_config_tenant_zone"


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def is_valid_time_zone(name) -> bool:
    """Whether *name* is an IANA zone this server can load.

    Membership of the tz database's own list rather than a bare ``ZoneInfo``
    call, because ``ZoneInfo`` also loads paths such as ``localtime`` that are
    whatever the host happens to be set to.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    if name not in _known_zones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def guard_time_zone(value, *, tenant=None, branch=None):
    """Write guard for ``display.timezone``: refuse anything that is not a zone.

    Registered from :class:`vs_config.apps.VsConfigConfig`, so the console's
    generic value endpoint and the school's display settings endpoint refuse the
    same values with the same error.
    """
    from .exceptions import InvalidConfigurationValue

    if not is_valid_time_zone(value):
        raise InvalidConfigurationValue(
            f"'{value}' is not a recognised time zone. Use an IANA name such as "
            f"{DEFAULT_TIME_ZONE}.",
            extra={"key": TIME_ZONE_KEY},
        )


def _config_tenant(tenant):
    """The tenant whose layer is read: ``None`` for the platform tenant itself."""
    if tenant is None or getattr(tenant, "kind", None) == "PLATFORM":
        return None
    return tenant


def _resolve_zone(tenant) -> ZoneInfo:
    from .conf import get_config

    name = get_config(TIME_ZONE_KEY, DEFAULT_TIME_ZONE, tenant=tenant)
    if not is_valid_time_zone(name):
        name = DEFAULT_TIME_ZONE
    return ZoneInfo(name)


def _memo_holders(tenant):
    """*tenant*, and the request's own instance of it when that is another object."""
    from vs_tenants.context import get_current_tenant

    holders = [tenant]
    ambient = get_current_tenant()
    if ambient is not None and ambient is not tenant and ambient.pk == tenant.pk:
        holders.insert(0, ambient)
    return holders


def tenant_zone(tenant) -> ZoneInfo:
    """The time zone *tenant* keeps its calendar in. ``None`` means the platform."""
    tenant = _config_tenant(tenant)
    if tenant is None:
        return _resolve_zone(None)
    holders = _memo_holders(tenant)
    zone = next(
        (getattr(holder, _MEMO_ATTRIBUTE) for holder in holders
         if hasattr(holder, _MEMO_ATTRIBUTE)),
        None,
    )
    if zone is None:
        zone = _resolve_zone(tenant)
    for holder in holders:
        setattr(holder, _MEMO_ATTRIBUTE, zone)
    return zone


def forget_tenant_zone(tenant) -> None:
    """Drop the memoised zone, so the next read sees a value just written."""
    if tenant is None:
        return
    for holder in _memo_holders(tenant):
        if hasattr(holder, _MEMO_ATTRIBUTE):
            delattr(holder, _MEMO_ATTRIBUTE)


def tenant_now(tenant) -> datetime.datetime:
    """The current instant as *tenant*'s wall clock shows it (aware)."""
    return timezone.now().astimezone(tenant_zone(tenant))


def tenant_today(tenant) -> datetime.date:
    """The calendar day it currently is at *tenant*."""
    return tenant_now(tenant).date()
