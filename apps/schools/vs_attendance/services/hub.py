"""The two Today screens: the administrator's hub and the teacher's own registers.

**The administrator's hub** answers, for one day, "how is the school doing and
who needs chasing". It is built from the registers that were actually
submitted: the attendance rate, the absences with the family's notice state, the
classes whose register is due and has not been taken, and the corrections
waiting. It is assembled in a fixed number of queries however many classes the
school has: the classes, the day's calendar, the rosters, the registers, the
marks, the reminders and the corrections are each read once for all of them.

**Scope.** A branch-bound caller sees their own branches' classes and a
whole-school caller sees every class; ``branch`` narrows either to one visible
branch. A class with no branch at a school that has several cannot have a
register (:class:`~..exceptions.ClassHasNoBranch`), so it is not on the hub's
lists. A whole-school reader is told which they are, so the gap can be fixed
rather than silently reading as "all registers taken".

**What is due.** A class owes a register on a day the school is open to it, that
has come at its branch, when somebody is placed in it. A class closed by a
holiday, or by a calendar entry for its branch or its levels, is not due and is
never listed as missing. When every class in scope is closed the hub carries
the reason and empty lists.

**The rate.** Present (late included) over present plus absent plus excused,
across the registers submitted that day, with suspended children counted
nowhere (:mod:`.rates`).

**The teacher's hub** lists the classes the caller leads in the active school
year, each with where its register stands, and nothing else. A teacher holding
neither ``mark_any`` nor ``attendance.correction.approve`` is sent there rather
than shown the school.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_academics.models import SchoolClass, SessionStatus
from vs_config.clock import branch_today, tenant_today
from vs_config.display import format_date
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

from ..constants import CorrectionStatus, Mark
from ..exceptions import DateInFuture
from ..models import ClassRegister, RegisterReminder, StudentMark
from . import corrections
from .days import DayCalendar
from .rates import rate_percent
from .registers import RegisterContext, register_status
from .roster import rosters_as_at
from .scope import placed_classes, school_branches

_CLASS_RELATIONS = ("session", "level", "branch", "class_teacher", "class_teacher__user")


def _active_classes(tenant, **extra) -> list:
    return list(
        SchoolClass.objects.filter(
            tenant=tenant, is_active=True, session__status=SessionStatus.ACTIVE, **extra,
        ).select_related(*_CLASS_RELATIONS),
    )


def _the_day(tenant, placed, on_date, default_today) -> tuple:
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


def _whole_day_closed(placed, days):
    if placed and all(not days[c.pk].is_open for c, _ in placed):
        return days[placed[0][0].pk].payload()
    return None


def _person(student) -> dict:
    return {"id": student.pk, "name": student.full_name, "student_number": student.student_number}


def admin_hub(tenant, user, *, on_date=None, branch=None, can_decide: bool) -> dict:
    """The administrator's hub for *on_date*, before Field Access is applied.

    *can_decide* is whether the caller holds ``attendance.correction.approve``,
    which alone shows the corrections waiting and the first of them.
    """
    branches = school_branches(tenant)
    several = len(branches) > 1
    visible = visible_branch_ids(user, tenant)
    classes = _active_classes(tenant)
    placed, unplaced = placed_classes(classes, branches, visible, branch)
    placed.sort(key=lambda pair: (
        pair[1].name.lower(), pair[0].level.order_index, pair[0].name.lower(), pair[0].pk,
    ))

    default_today = branch_today(tenant, branch) if branch is not None else None
    on_date, today_by_branch, calendar = _the_day(tenant, placed, on_date, default_today)
    days = {c.pk: calendar.status(c, home, on_date) for c, home in placed}
    due = [
        (c, home) for c, home in placed
        if days[c.pk].is_open and on_date <= today_by_branch[home.pk]
    ]
    closed = _whole_day_closed(placed, days)

    body = {
        "date": on_date,
        "date_label": format_date(on_date, tenant),
        "closed": closed,
        "kpis": {"rate": None, "absent": 0, "late": 0, "excused": 0, "not_taken": 0},
        "not_taken": [],
        "absent": [],
        "pending_corrections_preview": None,
        "branch_dimension": several,
        "classes_without_branch": (
            sorted(c.name for c in unplaced)
            if visible is WHOLE_TENANT and branch is None else []
        ),
    }

    if can_decide:
        body["kpis"]["corrections_waiting"] = corrections.pending_count(
            user, tenant, branch=branch,
        )
        first = (
            corrections.corrections_visible_to(user, tenant, only_own=False)
            .filter(status=CorrectionStatus.PENDING)
        )
        if branch is not None:
            first = first.filter(mark__register__branch=branch)
        first = first.first()
        if first is not None:
            body["pending_corrections_preview"] = corrections.correction_row(
                first, several_branches=several, viewer=user,
            )
    if closed is not None or not due:
        return body

    classes_due = [c for c, _ in due]
    rosters = rosters_as_at(tenant, classes_due, on_date)
    registers = {
        r.school_class_id: r
        for r in ClassRegister.objects.filter(
            tenant=tenant, date=on_date, school_class__in=classes_due,
        )
    }
    reminded = set(
        RegisterReminder.objects.filter(
            tenant=tenant, date=on_date, school_class__in=classes_due,
        ).values_list("school_class_id", flat=True),
    )
    marks_by_register: dict[int, list] = {}
    submitted = [r.pk for r in registers.values() if r.is_submitted]
    for mark in StudentMark.objects.filter(
        tenant=tenant, register_id__in=submitted,
    ).select_related("student", "reason_code"):
        marks_by_register.setdefault(mark.register_id, []).append(mark)

    tally = {code: 0 for code in Mark.values}
    not_taken, absent = [], []
    for school_class, home in due:
        roster = rosters[school_class.pk]
        if not roster:
            continue
        register = registers.get(school_class.pk)
        if register is None or not register.is_submitted:
            teacher = school_class.class_teacher
            not_taken.append({
                "class_id": school_class.pk,
                "class_name": school_class.name,
                "level_name": school_class.level.name,
                "class_teacher_name": teacher.user.full_name if teacher is not None else None,
                "branch": {"id": home.pk, "name": home.name} if several else None,
                "reminded": school_class.pk in reminded,
            })
            continue
        counted = {e.student.pk for e in roster if not e.suspended}
        for mark in marks_by_register.get(register.pk, []):
            if mark.student_id not in counted:
                continue
            tally[mark.mark] += 1
            if mark.mark in (Mark.ABSENT, Mark.EXCUSED):
                absent.append((school_class, mark))

    absent.sort(key=lambda pair: (
        pair[0].name.lower(), pair[1].student.last_name.lower(),
        pair[1].student.first_name.lower(), pair[1].student_id,
    ))
    present = tally[Mark.PRESENT] + tally[Mark.LATE]
    body["kpis"].update({
        "rate": rate_percent(present, tally[Mark.ABSENT], tally[Mark.EXCUSED]),
        "absent": tally[Mark.ABSENT],
        "late": tally[Mark.LATE],
        "excused": tally[Mark.EXCUSED],
        "not_taken": len(not_taken),
    })
    body["not_taken"] = not_taken
    body["absent"] = [
        {
            "student": _person(mark.student),
            "class_name": school_class.name,
            "mark": mark.mark,
            "reason_code": mark.reason_code_id,
            "reason_label": mark.reason_code.name if mark.reason_code_id else None,
            "note": mark.note,
            "notice_state": mark.notice_state,
            "notice_sent_at": mark.notice_sent_at,
        }
        for school_class, mark in absent
    ]
    return body


def teacher_hub(tenant, user, *, on_date: dt.date | None = None) -> dict:
    """The classes *user* leads, with where each register stands, before Field Access.

    A class the caller leads but cannot take a register for (it has no branch at
    a school with several, or sits in a branch they cannot see) is not listed.
    """
    branches = school_branches(tenant)
    visible = visible_branch_ids(user, tenant)
    classes = _active_classes(tenant, class_teacher__user=user)
    placed, _ = placed_classes(classes, branches, visible)
    placed.sort(key=lambda pair: (pair[0].level.order_index, pair[0].name.lower(), pair[0].pk))

    on_date, _, calendar = _the_day(tenant, placed, on_date, None)
    days = {c.pk: calendar.status(c, home, on_date) for c, home in placed}
    open_classes = [c for c, _ in placed if days[c.pk].is_open]
    rosters = rosters_as_at(tenant, open_classes, on_date)
    registers = {
        r.school_class_id: r
        for r in ClassRegister.objects.filter(
            tenant=tenant, date=on_date, school_class__in=[c for c, _ in placed],
        ).select_related("submitted_by")
    }

    cards = []
    for school_class, home in placed:
        ctx = RegisterContext(
            tenant=tenant, school_class=school_class, branch=home, date=on_date,
            day=days[school_class.pk], register=registers.get(school_class.pk),
            roster=rosters.get(school_class.pk, []),
        )
        status, counts = register_status(ctx)
        register = ctx.register
        submitted = register is not None and register.is_submitted
        cards.append({
            "class_id": school_class.pk,
            "class_name": school_class.name,
            "student_count": counts["total"],
            "status": status,
            "submitted_at": register.submitted_at if submitted else None,
            "submitted_by_name": (
                register.submitted_by.full_name
                if submitted and register.submitted_by is not None else None
            ),
        })
    return {
        "date": on_date,
        "date_label": format_date(on_date, tenant),
        "closed": _whole_day_closed(placed, days),
        "classes": cards,
    }
