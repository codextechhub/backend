"""Reading the staff register: who is on a branch's register on a day, and where it stands.

The staff register records what an administrator saw. There is no clock-in: a
member of staff is Present, Late (with the time they arrived) or Absent, and
nobody is told. It is one register per branch per school day.

**Who is on a branch's register.** A person is on the register of their home
branch (``StaffProfile.branch``) on every day their employment covers:

* somebody whose hire is still awaiting approval is not employed yet and is not
  listed; somebody hired after the date, or whose last working day is before it,
  is not listed;
* somebody whose last working day is on or after the date is listed with a note
  giving their status and that day (``exit``), because the register is the place
  an administrator notices a colleague is about to go;
* somebody off the roll (resigned or terminated) with no last working day has no
  day the register could end on and is not listed.

**One mark per person per day.** Additional postings
(``StaffProfile.additional_postings``) do not put a person on a second branch's
register: a teacher who works at Lekki on Monday and Ikeja on Tuesday is still
based at one of them, and is marked once a day on that branch's register.

**A person with no home branch cannot be marked.** At a school with several
branches such a person is based nowhere, so no register is theirs. Whole-school
readers see them in a separate group, locked with ``NO_HOME_BRANCH`` and the
sentence that says where to fix it; a branch-bound reader does not see the group
at all. At a school with one branch a person with no branch is that branch's
(``vs_rbac.scoping.only_branch_id_or_several``), so they are listed on it and
are markable, and the rule never bites there.

**On leave is read, not written.** Somebody on approved leave covering the date
(:class:`~schools.vs_staff.models.LeaveRequest`) is locked as ``ON_LEAVE``, cannot
be marked over, and carries the leave's id, type and last day for the link to the
leave record when the caller may read leave (see :func:`_row`). A leave that
ended yesterday leaves them markable today. Locked rows are kept out of every
count except ``on_leave``.

**Closed days.** A day the school closes, for everybody or for the branch, has no
staff register (:meth:`.days.DayCalendar.staff_status`). A closure narrowed to
some classes or levels does not close the staff day.

**Bounded cost.** The staff, their leave, the classes they lead, the registers and
the marks are each read once for every branch in scope, so the number of queries
does not grow with the number of branches or of people.

**Account state is not an attendance fact.** Nothing about a person's sign-in
(a lockout, a suspended account, a pending invitation) is read or served here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework.exceptions import NotFound

from schools.vs_academics.models import AcademicSession, SchoolClass, SessionStatus
from schools.vs_staff.constants import (
    OFF_ROLL_STATUSES,
    PERM_LEAVE_VIEW,
    EmploymentStatus,
    LeaveStatus,
)
from schools.vs_staff.models import LeaveRequest, StaffProfile
from vs_config.clock import branch_today, tenant_today
from vs_config.display import format_date
from vs_rbac.evaluator import has_permission
from vs_rbac.scoping import WHOLE_TENANT, only_branch_id_or_several, visible_branch_ids
from vs_tenants.references import find_branch_in_tenant

from ..constants import Mark, StaffLock
from ..exceptions import DateInFuture, DayClosedNoRegister, NoHomeBranch, OnLeave
from ..models import StaffDay, StaffMark
from .days import DayCalendar, DayStatus
from .marks import closed_sentence
from .registers import status_of
from .rules import read_edit_window_days
from .scope import school_branches

CODE_DATE_IN_FUTURE = "DATE_IN_FUTURE"


@dataclass
class StaffEntry:
    """One person on a register, with everything their row says about them."""

    staff: StaffProfile
    home: object = None
    leave: LeaveRequest | None = None
    leads: list = field(default_factory=list)
    mark: StaffMark | None = None

    @property
    def lock(self) -> str | None:
        """Why the row cannot be marked, or None."""
        if self.home is None:
            return StaffLock.NO_HOME_BRANCH.value
        if self.leave is not None:
            return StaffLock.ON_LEAVE.value
        return None

    @property
    def name(self) -> str:
        return self.staff.user.full_name


@dataclass
class StaffContext:
    """Everything a read or a write of one branch's staff register needs, resolved once."""

    tenant: object
    branch: object
    date: dt.date
    day: DayStatus
    staff_day: StaffDay | None
    entries: list = field(default_factory=list)
    several: bool = False
    whole_school: bool = False

    @property
    def is_submitted(self) -> bool:
        return self.staff_day is not None and self.staff_day.is_submitted


# ── the calendar ───────────────────────────────────────────────────────────

def staff_calendar(tenant, on_date: dt.date) -> DayCalendar:
    """The school's teaching days and closures for *on_date*, across every school year."""
    return DayCalendar(tenant, AcademicSession.objects.filter(tenant=tenant), on_date, on_date)


def branch_days(tenant, branches, on_date: dt.date) -> dict:
    """``{branch_id: DayStatus}`` for the staff of each branch on *on_date*.

    A day that has not yet come at a branch's own clock reads as closed with the
    code ``DATE_IN_FUTURE``, so a group for it says why it cannot be marked.
    """
    calendar = staff_calendar(tenant, on_date)
    days = {}
    for branch in branches:
        if on_date > branch_today(tenant, branch):
            days[branch.pk] = DayStatus(
                False, "That day has not come yet at this branch.", CODE_DATE_IN_FUTURE,
            )
        else:
            days[branch.pk] = calendar.staff_status(branch, on_date)
    return days


def assert_register_is_due(ctx: StaffContext) -> None:
    """Refuse a day that has not come, and a day the school is closed."""
    if ctx.date > branch_today(ctx.tenant, ctx.branch):
        raise DateInFuture()
    if not ctx.day.is_open:
        raise DayClosedNoRegister(closed_sentence(ctx.day))


# ── who is on the register ─────────────────────────────────────────────────

def employed_on(on_date: dt.date) -> Q:
    """The people whose employment covers *on_date*. See the module docstring."""
    return (
        ~Q(employment_status=EmploymentStatus.PENDING_APPROVAL)
        & (Q(hire_date__isnull=True) | Q(hire_date__lte=on_date))
        & (Q(exit_date__isnull=True) | Q(exit_date__gte=on_date))
        & ~(Q(employment_status__in=list(OFF_ROLL_STATUSES)) & Q(exit_date__isnull=True))
    )


def load_entries(tenant, on_date, branches, *, several, include_unhomed=False):
    """``({branch_id: [StaffEntry]}, [StaffEntry])`` for *branches* and the people with no home.

    The second list is the people based nowhere at a school with several
    branches, read only when *include_unhomed* says the caller may see them. At
    a school with one branch a person with no branch is that branch's and is
    listed on it. Fixed queries: the staff, their leave, the classes they lead
    and their marks.
    """
    by_branch = {branch.pk: [] for branch in branches}
    unhomed = []
    sole = branches[0] if (not several and branches) else None
    reach = Q(branch_id__in=list(by_branch))
    if sole is not None or (several and include_unhomed):
        reach |= Q(branch__isnull=True)
    if not by_branch and not (several and include_unhomed):
        return by_branch, unhomed

    people = list(
        StaffProfile.objects.filter(reach, employed_on(on_date), tenant=tenant)
        .select_related("user")
        .order_by(Lower("user__last_name"), Lower("user__first_name"), "pk"),
    )
    if not people:
        return by_branch, unhomed
    ids = [person.pk for person in people]

    leave = {}
    for row in LeaveRequest.objects.filter(
        tenant=tenant, staff_id__in=ids, status=LeaveStatus.APPROVED,
        start_date__lte=on_date, end_date__gte=on_date,
    ).order_by("end_date", "pk"):
        leave[row.staff_id] = row

    leads: dict[int, list] = {}
    for staff_id, name in SchoolClass.objects.filter(
        tenant=tenant, class_teacher_id__in=ids, is_active=True,
        session__status=SessionStatus.ACTIVE,
    ).order_by("name", "pk").values_list("class_teacher_id", "name"):
        leads.setdefault(staff_id, []).append(name)

    marks = {
        mark.staff_id: mark
        for mark in StaffMark.objects.filter(tenant=tenant, date=on_date, staff_id__in=ids)
    }

    homes = {branch.pk: branch for branch in branches}
    for person in people:
        home = homes.get(person.branch_id) if person.branch_id is not None else sole
        entry = StaffEntry(
            staff=person, home=home, leave=leave.get(person.pk),
            leads=leads.get(person.pk, []), mark=marks.get(person.pk),
        )
        if home is None:
            unhomed.append(entry)
        else:
            by_branch[home.pk].append(entry)
    return by_branch, unhomed


# ── one branch, for a write ────────────────────────────────────────────────

def load_context(tenant, user, branch_id, on_date: dt.date) -> StaffContext:
    """Resolve the branch, the day, the register and the people on it.

    Raises 404 for a branch that is not this school's or that the caller cannot
    see, so a branch admin cannot tell another branch's register from one that
    does not exist.
    """
    branch = find_branch_in_tenant(tenant, branch_id)
    visible = visible_branch_ids(user, tenant)
    if branch is None or (visible is not WHOLE_TENANT and branch.pk not in visible):
        raise NotFound("No such branch at this school.")
    several = only_branch_id_or_several(tenant) is None
    day = branch_days(tenant, [branch], on_date)[branch.pk]
    staff_day = (
        StaffDay.objects.filter(tenant=tenant, branch=branch, date=on_date)
        .select_related("submitted_by").first()
    )
    entries = []
    if day.is_open:
        by_branch, _ = load_entries(tenant, on_date, [branch], several=several)
        entries = by_branch[branch.pk]
    return StaffContext(
        tenant=tenant, branch=branch, date=on_date, day=day, staff_day=staff_day,
        entries=entries, several=several, whole_school=visible is WHOLE_TENANT,
    )


def writable_entry(ctx: StaffContext, staff_id) -> StaffEntry:
    """The row for *staff_id* on this register, or the refusal that explains why not.

    A person with no home branch at a school with several is refused with
    ``NO_HOME_BRANCH`` to a caller who can see them (whole-school), and read as
    not on the register to anyone else. Somebody on approved leave is refused
    with ``ON_LEAVE``.
    """
    entry = next((e for e in ctx.entries if e.staff.pk == staff_id), None)
    if entry is None:
        if ctx.several and ctx.whole_school:
            stray = (
                StaffProfile.objects.filter(
                    employed_on(ctx.date), tenant=ctx.tenant, pk=staff_id,
                    branch__isnull=True,
                ).select_related("user").first()
            )
            if stray is not None:
                raise NoHomeBranch(no_home_sentence(stray.user.full_name), staff_id=stray.pk)
        raise NotFound("That person is not on this register.")
    if entry.lock == StaffLock.ON_LEAVE.value:
        raise OnLeave(
            f"{entry.name} is on approved leave, which is filled in from Staff "
            f"Management and cannot be marked over.",
            staff_id=entry.staff.pk,
        )
    return entry


def no_home_sentence(name: str) -> str:
    return f"Give {name} a home branch in Staff Management before marking them."


# ── the document ───────────────────────────────────────────────────────────

def can_read_leave(user, tenant) -> bool:
    """Whether *user* may read leave requests (``school.leave.view``), asked once per request."""
    return has_permission(user, PERM_LEAVE_VIEW, tenant=tenant)


def _row(entry: StaffEntry, ctx_tenant, branch, *, show_leave: bool = False) -> dict:
    """One person's line on the register, as a plain dict before Field Access.

    A locked On-leave row always says it is locked and why (``lock_reason`` and
    ``lock_note``), but its ``leave`` object (id, type, last day) is a fact of
    the leave record, not of attendance: what kind of leave a colleague is on is
    for somebody who may read leave, and the link it feeds opens nothing for a
    caller who cannot. So the object is present only when *show_leave* is true
    (the caller holds ``school.leave.view``, checked once for the request by the
    caller of this function, never per row) and the key is absent otherwise, so
    a screen shows "On leave" with no link. A row that is not on leave carries
    ``leave: None`` for everybody. Marking is unaffected: the ``ON_LEAVE`` refusal
    applies to every caller.
    """
    staff = entry.staff
    locked = entry.lock is not None
    stored = None if locked else entry.mark
    leave = entry.leave
    exit_block = None
    if staff.exit_date is not None:
        exit_block = {
            "label": staff.get_employment_status_display(),
            "last_working_day": staff.exit_date,
            "last_working_day_label": format_date(staff.exit_date, ctx_tenant, branch=branch),
        }
    note = None
    if entry.lock == StaffLock.NO_HOME_BRANCH.value:
        note = no_home_sentence(entry.name)
    elif entry.lock == StaffLock.ON_LEAVE.value:
        note = (
            "On approved leave. It is filled in from Staff Management and "
            "cannot be marked over."
        )
    row = {
        "staff": {
            "id": staff.pk, "name": entry.name, "staff_number": staff.staff_number,
        },
        "job_title": staff.job_title,
        "owner_user_id": staff.user_id,
        "leads": entry.leads,
        "locked": locked,
        "lock_reason": entry.lock,
        "lock_note": note,
        "mark": stored.mark if stored else None,
        "arrived_at": stored.arrived_at if stored else None,
        "note": stored.note if stored else "",
        "exit": exit_block,
    }
    if leave is None:
        row["leave"] = None
    elif show_leave:
        row["leave"] = {
            "id": leave.pk, "type": leave.leave_type,
            "type_label": leave.get_leave_type_display(), "end_date": leave.end_date,
        }
    return row


def _counts(rows) -> dict:
    live = [row for row in rows if not row["locked"]]
    tally = {code: 0 for code in Mark.values}
    unmarked = 0
    for row in live:
        if row["mark"] is None:
            unmarked += 1
        else:
            tally[row["mark"]] += 1
    return {
        "total": len(live),
        "present": tally[Mark.PRESENT] + tally[Mark.LATE],
        "late": tally[Mark.LATE],
        "absent": tally[Mark.ABSENT],
        "on_leave": sum(1 for row in rows if row["lock_reason"] == StaffLock.ON_LEAVE.value),
        "unmarked": unmarked,
    }


def edit_window_open(ctx: StaffContext, window_days: int) -> bool:
    """Whether the register's day is still within its edit window, in the branch's own time."""
    return branch_today(ctx.tenant, ctx.branch) <= ctx.date + dt.timedelta(days=window_days)


def group_payload(
    ctx: StaffContext, window_days: int | None = None, *, show_leave: bool = False,
) -> dict:
    """One branch's staff register, before Field Access is applied to it.

    *show_leave* is whether the caller may read leave requests; without it a
    locked On-leave row carries no ``leave`` object (see :func:`_row`).
    """
    rows = [_row(entry, ctx.tenant, ctx.branch, show_leave=show_leave) for entry in ctx.entries]
    counts = _counts(rows)
    staff_day = ctx.staff_day
    submitted = ctx.is_submitted
    if submitted and window_days is None:
        window_days = read_edit_window_days(ctx.tenant)
    return {
        "branch": {"id": ctx.branch.pk, "name": ctx.branch.name},
        "closed": ctx.day.payload(),
        "status": status_of(ctx, counts),
        "submitted_at": staff_day.submitted_at if submitted else None,
        "submitted_by_name": (
            staff_day.submitted_by.full_name
            if submitted and staff_day.submitted_by is not None else None
        ),
        "edit_window_open": edit_window_open(ctx, window_days) if submitted else None,
        "counts": counts,
        "rows": rows,
    }


def read_staff_registers(tenant, user, *, on_date=None, branch=None) -> dict:
    """The staff registers of one day in the caller's scope, before Field Access is applied.

    One group per branch the caller can see (or the one named by *branch*),
    and for a whole-school reader at a school with several branches the people
    with no home branch in a group of their own.
    """
    branches = school_branches(tenant)
    several = len(branches) > 1
    visible = visible_branch_ids(user, tenant)
    whole = visible is WHOLE_TENANT
    in_scope = [
        b for b in branches
        if (whole or b.pk in visible) and (branch is None or b.pk == branch.pk)
    ]

    if on_date is None:
        on_date = branch_today(tenant, branch) if branch is not None else tenant_today(tenant)
    latest = max(
        (branch_today(tenant, b) for b in in_scope), default=tenant_today(tenant),
    )
    if on_date > latest:
        raise DateInFuture()

    days = branch_days(tenant, in_scope, on_date)
    open_branches = [b for b in in_scope if days[b.pk].is_open]
    show_unhomed = several and whole and branch is None and bool(open_branches)
    by_branch, unhomed = load_entries(
        tenant, on_date, open_branches, several=several, include_unhomed=show_unhomed,
    )
    staff_days = {
        row.branch_id: row
        for row in StaffDay.objects.filter(
            tenant=tenant, date=on_date, branch_id__in=[b.pk for b in open_branches],
        ).select_related("submitted_by")
    }

    window_days = (
        read_edit_window_days(tenant) if any(d.is_submitted for d in staff_days.values())
        else None
    )
    show_leave = can_read_leave(user, tenant)
    groups = [
        group_payload(
            StaffContext(
                tenant=tenant, branch=b, date=on_date, day=days[b.pk],
                staff_day=staff_days.get(b.pk), entries=by_branch.get(b.pk, []),
                several=several, whole_school=whole,
            ),
            window_days, show_leave=show_leave,
        )
        for b in in_scope
    ]
    closed = groups[0]["closed"] if groups and all(g["closed"] for g in groups) else None
    no_home = None
    if show_unhomed:
        rows = [_row(entry, tenant, None, show_leave=show_leave) for entry in unhomed]
        no_home = {"count": len(rows), "rows": rows}
    return {
        "date": on_date,
        "date_label": format_date(on_date, tenant),
        "closed": closed,
        "branch_dimension": several,
        "groups": groups,
        "no_home_branch": no_home,
    }
