"""Reading one class's register for one day.

A register is read as a single document: the class, the day and whether the
school is open, the rows, the counts, who took it, the history of changes to it,
and what submitting it would do. The same body serves the daily register screen
and the phone frames.

**The status is worked out on every read, never stored.**

* ``CLOSED_DAY``: the school is not open to this class that day.
* ``SUBMITTED``: the register has been submitted.
* ``NOT_STARTED``: no unlocked row is marked.
* ``READY``: every unlocked row is marked and the register can be submitted.
* ``IN_PROGRESS``: some are marked and some are not.

**Locked rows are kept out of every count.** A suspended child is listed so the
register is complete, but carries no mark and is counted nowhere.

**Present includes late.** A late child is present, so ``present`` counts both,
and ``late`` is the part of it that arrived late. Absent and excused are their
own counts. ``unmarked`` is what stands between the register and a submit.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from schools.vs_staff.constants import LeaveStatus
from schools.vs_staff.models import LeaveRequest
from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import Mark, RegisterStatus
from ..models import ClassRegister, MarkChange, StudentMark
from . import notices
from .access import open_class
from .days import DayStatus, school_day
from schools.vs_students.services.roster import RosterEntry, roster_as_at
from .rules import read_edit_window_days


@dataclass
class RegisterContext:
    """Everything a read or a write of one register needs, resolved once."""

    tenant: object
    school_class: object
    branch: object
    date: dt.date
    day: DayStatus
    register: ClassRegister | None
    roster: list[RosterEntry] = field(default_factory=list)

    @property
    def is_submitted(self) -> bool:
        return self.register is not None and self.register.is_submitted


def load_context(tenant, user, class_id, on_date: dt.date) -> RegisterContext:
    """Resolve the class, the branch, the day, the register and the roster.

    Raises 404 for a class that is not the caller's to open, and
    ``CLASS_HAS_NO_BRANCH`` for a shared class at a school with several
    branches. A closed day has no roster.
    """
    school_class, branch = open_class(tenant, user, class_id)
    day = school_day(tenant, school_class, branch, on_date)
    register = ClassRegister.objects.filter(
        tenant=tenant, school_class=school_class, date=on_date,
    ).select_related("submitted_by", "on_behalf_of", "on_behalf_of__user").first()
    roster = roster_as_at(tenant, school_class, on_date) if day.is_open else []
    return RegisterContext(
        tenant=tenant, school_class=school_class, branch=branch, date=on_date,
        day=day, register=register, roster=roster,
    )


def edit_window_closes_on(ctx: RegisterContext) -> dt.date:
    """The last day a submitted register can still be changed in place."""
    return ctx.date + dt.timedelta(days=read_edit_window_days(ctx.tenant))


def window_is_open(ctx: RegisterContext) -> bool:
    """Whether the register's day is still within its edit window, in the branch's own time."""
    return branch_today(ctx.tenant, ctx.branch) <= edit_window_closes_on(ctx)


def _person(user) -> str:
    return user.full_name if user is not None else ""


def _marks_by_student(ctx: RegisterContext) -> dict:
    if ctx.register is None:
        return {}
    return {
        mark.student_id: mark
        for mark in StudentMark.objects.filter(
            tenant=ctx.tenant, register=ctx.register,
        ).select_related("reason_code")
    }


def _row(entry: RosterEntry, mark) -> dict:
    locked = entry.suspended
    stored = None if locked else mark
    return {
        "student_id": entry.student.pk,
        "name": entry.student.full_name,
        "student_number": entry.student.student_number,
        "locked": locked,
        "mark": stored.mark if stored else None,
        "arrived_at": stored.arrived_at if stored else None,
        "reason_code": stored.reason_code_id if stored else None,
        "reason_label": stored.reason_code.name if stored and stored.reason_code_id else None,
        "note": stored.note if stored else "",
        "notice_state": stored.notice_state if stored else "NONE",
        "notice_sent_at": stored.notice_sent_at if stored else None,
    }


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
        "excused": tally[Mark.EXCUSED],
        "unmarked": unmarked,
        "locked": len(rows) - len(live),
    }


def status_of(ctx: RegisterContext, counts: dict) -> str:
    if not ctx.day.is_open:
        return RegisterStatus.CLOSED_DAY.value
    if ctx.is_submitted:
        return RegisterStatus.SUBMITTED.value
    if counts["total"] == 0 or counts["unmarked"] == counts["total"]:
        return RegisterStatus.NOT_STARTED.value
    if counts["unmarked"] == 0:
        return RegisterStatus.READY.value
    return RegisterStatus.IN_PROGRESS.value


def register_status(ctx: RegisterContext) -> tuple[str, dict]:
    """The status and the counts of one register, without the rest of its document.

    For a screen that lists many registers and shows only where each stands.
    """
    marks = _marks_by_student(ctx)
    counts = _counts([_row(entry, marks.get(entry.student.pk)) for entry in ctx.roster])
    return status_of(ctx, counts), counts


def _taken_by(ctx: RegisterContext):
    register = ctx.register
    if register is None or not register.is_submitted:
        return None
    leave = None
    teacher = register.on_behalf_of
    if teacher is not None:
        row = (
            LeaveRequest.objects.filter(
                tenant=ctx.tenant, staff=teacher, status=LeaveStatus.APPROVED,
                start_date__lte=ctx.date, end_date__gte=ctx.date,
            )
            .order_by("-start_date").first()
        )
        if row is not None:
            leave = {
                "leave_type": row.leave_type,
                "leave_type_label": row.get_leave_type_display(),
                "ends_on": row.end_date,
            }
    return {
        "submitted_by": _person(register.submitted_by),
        "submitted_at": register.submitted_at,
        "on_behalf_of": _person(teacher.user) if teacher is not None else None,
        "on_leave": leave,
    }


def _history(ctx: RegisterContext) -> list:
    if ctx.register is None:
        return []
    changes = (
        MarkChange.objects.filter(tenant=ctx.tenant, mark__register=ctx.register)
        .select_related("mark__student", "changed_by")
        .order_by("changed_at", "id")
    )
    return [
        {
            "student_id": change.mark.student_id,
            "student_name": change.mark.student.full_name,
            "from_mark": change.from_mark or None,
            "to_mark": change.to_mark,
            "changed_by_name": _person(change.changed_by),
            "changed_at": change.changed_at,
            "via": change.via,
            "note": change.note,
        }
        for change in changes
    ]


def _submit_preview(ctx: RegisterContext, rows):
    """What submitting would do about guardian notices; null once it is done."""
    if ctx.is_submitted or not ctx.day.is_open:
        return None
    absent = [row["student_id"] for row in rows if row["mark"] == Mark.ABSENT and not row["locked"]]
    register_id = ctx.register.pk if ctx.register is not None else None
    return notices.preview(ctx.tenant, ctx.date, register_id, absent)


def register_payload(ctx: RegisterContext) -> dict:
    """The body of a register read, before Field Access is applied to it."""
    marks = _marks_by_student(ctx)
    rows = [_row(entry, marks.get(entry.student.pk)) for entry in ctx.roster]
    counts = _counts(rows)
    teacher = ctx.school_class.class_teacher
    several = only_branch_id_or_several(ctx.tenant) is None
    submitted = ctx.is_submitted
    return {
        "school_class": {
            "id": ctx.school_class.pk,
            "name": ctx.school_class.name,
            "class_teacher_name": _person(teacher.user) if teacher is not None else None,
            "branch": (
                {"id": ctx.branch.pk, "name": ctx.branch.name} if several else None
            ),
        },
        "date": ctx.date,
        "date_label": format_date(ctx.date, ctx.tenant, branch=ctx.branch),
        "closed": ctx.day.payload(),
        "status": status_of(ctx, counts),
        "roster_empty": ctx.day.is_open and not ctx.roster,
        "counts": counts,
        "taken_by": _taken_by(ctx),
        "edit_window_open": window_is_open(ctx) if submitted else None,
        "rows": rows,
        "history": _history(ctx),
        "submit_preview": _submit_preview(ctx, rows),
    }


def read_register(tenant, user, class_id, on_date: dt.date) -> dict:
    """The register body for one class and day, for a caller who may open the class."""
    return register_payload(load_context(tenant, user, class_id, on_date))
