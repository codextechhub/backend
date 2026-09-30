"""Template filters that print a date or a time the way a tenant reads them.

::

    {% load display_dates %}
    {{ invoice.due_date|display_date:tenant }}       29 Sep 2026
    {{ delivery.sent_at|display_datetime:branch }}   29 Sep 2026, 2:30 pm
    {{ slot.starts_at|display_time:tenant }}         2:30 pm
    {{ period.start|display_month:tenant }}          Sep 2026

The argument is a tenant or a ``vs_tenants.Branch``. A branch prints in its
tenant's date format and clock, and places an instant on its own wall clock
where it keeps a zone of its own, else its school's. No argument reads the
platform's values. Each filter is :mod:`vs_config.display` under another name,
so a template and the Python that builds its context cannot drift.

The library is named for the display settings rather than for a school because
``vs_config`` is an engine app: a finance or procurement document loads it too.
"""
from django import template

from vs_config import display

register = template.Library()


def _tenant_and_branch(holder):
    """``(tenant, branch)`` for a filter argument: a tenant, a branch or nothing."""
    from vs_tenants.models import Branch

    if isinstance(holder, Branch):
        return holder.tenant, holder
    return holder, None


@register.filter
def display_date(value, holder=None):
    tenant, branch = _tenant_and_branch(holder)
    return display.format_date(value, tenant, branch=branch)


@register.filter
def display_datetime(value, holder=None):
    tenant, branch = _tenant_and_branch(holder)
    return display.format_datetime(value, tenant, branch=branch)


@register.filter
def display_time(value, holder=None):
    tenant, branch = _tenant_and_branch(holder)
    return display.format_time(value, tenant, branch=branch)


@register.filter
def display_month(value, holder=None):
    tenant, branch = _tenant_and_branch(holder)
    return display.format_month(value, tenant, branch=branch)
