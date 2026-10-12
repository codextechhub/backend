"""Which classes, in which branches, a caller's attendance screens cover.

A class names its own branch, or is the only branch's at a school that has one.
A class with neither has no branch to take a register in
(:class:`~..exceptions.ClassHasNoBranch`), so it is returned apart from the
ones the screens list. A branch-bound caller sees their own branches' classes
and a whole-school caller sees every class; a named branch narrows either to
one.
"""
from __future__ import annotations

from schools.vs_academics.models import SchoolClass, SessionStatus
from vs_config.clock import branch_today, tenant_today
from vs_rbac.scoping import WHOLE_TENANT
from vs_tenants.exceptions import BranchlessTenantError
from vs_tenants.models import Branch

from ..exceptions import DateInFuture
from .days import DayCalendar

CLASS_RELATIONS = ("session", "level", "branch", "class_teacher", "class_teacher__user")


def school_branches(tenant) -> list:
    """Every branch of *tenant*, by name. A school with none is a data fault."""
    branches = list(Branch.all_objects.filter(tenant=tenant).order_by("name", "pk"))
    if not branches:
        raise BranchlessTenantError(tenant)
    return branches


def placed_classes(classes, branches, visible, branch=None):
    """``[(class, branch)]`` for the classes the caller can see, and the ones with no branch.

    A class with no branch of its own is the school's only branch's at a school
    with one, and is returned apart otherwise.
    """
    only = branches[0] if len(branches) == 1 else None
    placed, unplaced = [], []
    for school_class in classes:
        home = school_class.branch if school_class.branch_id is not None else only
        if home is None:
            unplaced.append(school_class)
            continue
        if visible is not WHOLE_TENANT and home.pk not in visible:
            continue
        if branch is not None and home.pk != branch.pk:
            continue
        placed.append((school_class, home))
    return placed, unplaced


def active_classes(tenant, **extra) -> list:
    """The active classes of the active school year, with what the screens read of them."""
    return list(
        SchoolClass.objects.filter(
            tenant=tenant, is_active=True, session__status=SessionStatus.ACTIVE, **extra,
        ).select_related(*CLASS_RELATIONS),
    )


def resolve_day(tenant, placed, on_date, default_today) -> tuple:
    """``(date, {branch_id: today}, calendar)`` for the classes, refusing a day not yet come."""
    today_by_branch = {
        home.pk: branch_today(tenant, home) for _, home in placed
    }
    if on_date is None:
        on_date = default_today or tenant_today(tenant)
    latest = max(today_by_branch.values(), default=tenant_today(tenant))
    if on_date > latest:
        raise DateInFuture()
    sessions = {school_class.session_id: school_class.session for school_class, _ in placed}
    calendar = DayCalendar(tenant, sessions.values(), on_date, on_date)
    return on_date, today_by_branch, calendar


def whole_day_closed(placed, days):
    """The ``closed`` block when every class in scope is closed, else None."""
    if placed and all(not days[c.pk].is_open for c, _ in placed):
        return days[placed[0][0].pk].payload()
    return None
