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

**A branch may keep its own zone.** A school whose Nairobi branch sits three
hours east of its Lagos head office sets a branch value of the same key, and
anything that belongs to that branch (a student, a posting, an invoice, a
leave request) reads its day from :func:`branch_today` and its clock from
:func:`branch_now`. A branch with no value of its own follows the school, so
for the ordinary school, whose branches all share one zone, the branch
functions answer exactly what the tenant ones do. ``branch=None`` is the
school's own zone: a row shared across the school has no branch clock.

:func:`branch_day_q` is the queryset form, for a filter over rows of several
branches ("overdue" across a school's invoices): each row is judged against
its own branch's day.

A stored instant stays ``timezone.now()``. Only a *day* or a *wall-clock
reading* comes from here.

**Resolution.** The branch's own value (for the branch functions), then the
tenant's, then the platform value, then :data:`DEFAULT_TIME_ZONE`
(Africa/Lagos), which is also the definition's default. ``None`` and the
platform tenant itself both read the platform layer, so a caller with no
tenant in reach still gets the platform's day rather than UTC's.

**Why here.** The value is a ``vs_config`` definition read through
:func:`vs_config.conf.get_config`, and ``vs_config`` depends on no domain app,
so every engine and every school app can import this without a cycle.
``core`` would need to reach up into ``vs_config`` to answer, which is the
wrong direction for the lowest layer.

**Caching.** A tenant's zone costs two queries (the definition, then every
value that can apply to it), and the same two read every branch that keeps its
own zone, whatever the number of branches. Both are memoised on the tenant
instance. ``request.tenant``
lives for one request and a task loads its own, so a memo never outlives the
unit of work that read it. Another instance of the request's own tenant (a
row's ``row.tenant``, say) shares the memo held on the request's instance, so
a list reading "today" per row asks the configuration once, not once per row.
A write through the display settings endpoint calls
:func:`forget_tenant_zone`, which drops both memos, so the refreshed answer in
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
_BRANCH_MEMO_ATTRIBUTE = "_vs_config_branch_zones"


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


def _read_zones(tenant):
    """``(tenant zone, {branch id: zone})`` for a business *tenant*, in two queries.

    The definition, then every value that can matter to this tenant at once:
    the platform's, the tenant's and each of its branches'. Reading the branch
    values beside the tenant's costs nothing extra, so a request that asks for
    "today" anywhere has every branch's zone to hand as well.
    """
    from django.db.models import Q

    from .models import ConfigurationDefinition, ConfigurationValue

    definition = ConfigurationDefinition.objects.filter(
        key=TIME_ZONE_KEY, is_active=True,
    ).first()
    if definition is None:
        return ZoneInfo(DEFAULT_TIME_ZONE), {}
    rows = ConfigurationValue.all_objects.filter(definition=definition).filter(
        Q(scope_key__in=["platform", f"tenant:{tenant.pk}"])
        | Q(tenant_id=tenant.pk, branch__isnull=False),
    ).values_list("scope_key", "branch_id", "value")
    layers, branches = {}, {}
    for scope_key, branch_id, value in rows:
        if branch_id is not None:
            if is_valid_time_zone(value):
                branches[branch_id] = ZoneInfo(value)
        else:
            layers[scope_key] = value
    name = layers.get(f"tenant:{tenant.pk}")
    if name is None:
        name = layers.get("platform")
    if name is None:
        name = definition.default_value
    if not is_valid_time_zone(name):
        name = DEFAULT_TIME_ZONE
    return ZoneInfo(name), branches


def _remembered(tenant):
    """``(zone, branch zones)`` for a business *tenant*, memoised on its holders."""
    holders = _memo_holders(tenant)
    for holder in holders:
        if hasattr(holder, _MEMO_ATTRIBUTE) and hasattr(holder, _BRANCH_MEMO_ATTRIBUTE):
            zone = getattr(holder, _MEMO_ATTRIBUTE)
            branches = getattr(holder, _BRANCH_MEMO_ATTRIBUTE)
            break
    else:
        zone, branches = _read_zones(tenant)
    for holder in holders:
        setattr(holder, _MEMO_ATTRIBUTE, zone)
        setattr(holder, _BRANCH_MEMO_ATTRIBUTE, branches)
    return zone, branches


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
    return _remembered(tenant)[0]


def forget_tenant_zone(tenant) -> None:
    """Drop the memoised zones, the tenant's and its branches'.

    The next read then sees a value just written, in the same request.
    """
    if tenant is None:
        return
    for holder in _memo_holders(tenant):
        for attribute in (_MEMO_ATTRIBUTE, _BRANCH_MEMO_ATTRIBUTE):
            if hasattr(holder, attribute):
                delattr(holder, attribute)


def tenant_now(tenant) -> datetime.datetime:
    """The current instant as *tenant*'s wall clock shows it (aware)."""
    return timezone.now().astimezone(tenant_zone(tenant))


def tenant_today(tenant) -> datetime.date:
    """The calendar day it currently is at *tenant*."""
    return tenant_now(tenant).date()


# -- A branch's own clock ---------------------------------------------------- #

def _branch_id(branch):
    """The id of *branch*: a ``Branch``, an id, an id's digits, or ``None``."""
    if branch is None or isinstance(branch, bool):
        return None
    if isinstance(branch, int):
        return branch
    if isinstance(branch, str):
        return int(branch) if branch.strip().isdigit() else None
    return getattr(branch, "pk", None)


def branch_zones(tenant) -> dict:
    """``{branch id: ZoneInfo}`` for every branch of *tenant* with its own zone.

    A branch that follows the school is absent. Read in the same two queries
    as the tenant's own zone and memoised beside it (see the module
    docstring). A stored value that is not a zone is left out, so that branch
    follows the school.
    """
    tenant = _config_tenant(tenant)
    if tenant is None:
        return {}
    return _remembered(tenant)[1]


def _own_zone(tenant, branch):
    """``(tenant, zone)``: *branch*'s own zone, or ``None`` when it follows the school."""
    if tenant is None and getattr(branch, "tenant_id", None) is not None:
        tenant = branch.tenant
    branch_id = _branch_id(branch)
    if branch_id is None:
        return tenant, None
    return tenant, branch_zones(tenant).get(branch_id)


def branch_zone(tenant, branch) -> ZoneInfo:
    """The zone *branch* keeps its calendar in: its own, else *tenant*'s.

    *branch* is a ``Branch``, a branch id or ``None``; ``None`` is the school's
    zone, and so is a branch that is not one of *tenant*'s. A *tenant* of
    ``None`` with a ``Branch`` instance reads the branch's own tenant.
    """
    tenant, own = _own_zone(tenant, branch)
    return own if own is not None else tenant_zone(tenant)


def branch_now(tenant, branch) -> datetime.datetime:
    """The current instant as *branch*'s wall clock shows it (aware).

    The instant is :func:`tenant_now`'s, so there is one "now" for a tenant
    and its branches, read in the branch's zone where it keeps its own.
    """
    tenant, own = _own_zone(tenant, branch)
    now = tenant_now(tenant)
    return now if own is None else now.astimezone(own)


def branch_today(tenant, branch) -> datetime.date:
    """The calendar day it currently is at *branch* (the school's without one)."""
    return branch_now(tenant, branch).date()


def branch_day_q(tenant, field, on_day):
    """A ``Q`` judging each row by the day at its own branch.

    *on_day* takes a ``date`` and returns the ``Q`` for that day, such as
    ``lambda day: Q(due_date__lt=day)``; *field* is the lookup path to the
    row's branch (``"branch"``, ``"staff__branch"``). Rows at a branch whose
    day differs from the school's are judged on theirs, every other row
    (shared rows with no branch included) on the school's.

    Where no branch's day differs from the school's, which is every school
    whose branches share its zone and every school at most hours of the day,
    the answer is exactly ``on_day(tenant_today(tenant))``.
    """
    from collections import defaultdict

    from django.db.models import Q

    instant = tenant_now(tenant)
    school_day = instant.date()
    groups = defaultdict(list)
    for branch_id, zone in branch_zones(tenant).items():
        day = instant.astimezone(zone).date()
        if day != school_day:
            groups[day].append(branch_id)
    if not groups:
        return on_day(school_day)
    elsewhere = sorted(branch_id for ids in groups.values() for branch_id in ids)
    condition = on_day(school_day) & ~Q(**{f"{field}__in": elsewhere})
    for day in sorted(groups):
        condition |= on_day(day) & Q(**{f"{field}__in": sorted(groups[day])})
    return condition
