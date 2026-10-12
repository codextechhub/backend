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

**Missed a lesson.** When lesson marking is on for a level of a class in scope,
the hub also counts the students who were in school in the morning and absent
from a lesson (``kpis.missed_a_lesson``) and lists them (``missed_lessons``,
:mod:`.missed`). Both are left out of the body entirely when no class in scope is
in a level that marks lessons. A missed lesson is never part of the rate or of
any absence count.

**The teacher's hub** lists the classes the caller leads in the active school
year, each with where its register stands, and the caller's own lessons of the
day (``lessons``, with ``lesson_marking_on``). A teacher holding
neither ``mark_any`` nor ``attendance.correction.approve`` is sent there rather
than shown the school.
"""
from __future__ import annotations

import datetime as dt

from vs_config.clock import branch_today, tenant_today
from vs_config.display import format_date
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

from ..constants import CorrectionStatus, Mark
from ..models import ClassRegister, RegisterReminder, StudentMark
from . import corrections, lessons, missed
from .rates import rate_percent
from .registers import RegisterContext, register_status
from .roster import rosters_as_at
from .scope import (
    active_classes,
    placed_classes,
    resolve_day,
    school_branches,
    whole_day_closed,
)


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
    classes = active_classes(tenant)
    placed, unplaced = placed_classes(classes, branches, visible, branch)
    placed.sort(key=lambda pair: (
        pair[1].name.lower(), pair[0].level.order_index, pair[0].name.lower(), pair[0].pk,
    ))

    default_today = branch_today(tenant, branch) if branch is not None else None
    on_date, today_by_branch, calendar = resolve_day(tenant, placed, on_date, default_today)
    days = {c.pk: calendar.status(c, home, on_date) for c, home in placed}
    due = [
        (c, home) for c, home in placed
        if days[c.pk].is_open and on_date <= today_by_branch[home.pk]
    ]
    closed = whole_day_closed(placed, days)
    levels = lessons.marking_levels(tenant)
    marks_lessons = {
        c.pk for c, _ in placed if lessons.level_marks_lessons(c, levels)
    }

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

    if marks_lessons:
        body["kpis"]["missed_a_lesson"] = 0
        body["missed_lessons"] = []

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
    if marks_lessons:
        rows = _missed_lessons(tenant, on_date, due, rosters, marks_lessons)
        body["kpis"]["missed_a_lesson"] = len(rows)
        body["missed_lessons"] = rows
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


def _missed_lessons(tenant, on_date, due, rosters, marks_lessons) -> list:
    """The day's missed lessons among the classes that owe a register and mark lessons.

    A student suspended on the day is left out, as they are from every count.
    """
    counted = {
        school_class.pk: {e.student.pk for e in rosters[school_class.pk] if not e.suspended}
        for school_class, _ in due if school_class.pk in marks_lessons
    }
    found = missed.missed_on(tenant, on_date, list(counted))
    return [
        {
            "student": _person(row.student),
            "class_name": row.register.school_class.name,
            "period_label": row.register.period_label,
            "subject": row.register.subject.name,
            "lesson_submitted_at": row.register.submitted_at,
            "morning_mark": row.morning,
        }
        for row in found
        if row.student_id in counted[row.register.school_class_id]
    ]


def teacher_hub(tenant, user, *, on_date: dt.date | None = None) -> dict:
    """The classes *user* leads and the lessons they teach, before Field Access.

    Each class is listed with where its register stands. A class the caller
    leads but cannot take a register for (it has no branch at a school with
    several, or sits in a branch they cannot see) is not listed.

    ``lessons`` are the lessons the timetable gives the caller on the day, in
    classes whose level marks lessons and whose timetable is published, each
    with where its register stands. The list is empty, never absent, and
    ``lesson_marking_on`` says whether any level marks lessons at all, so a
    screen can tell a teacher with no lessons from a school that marks none.
    """
    branches = school_branches(tenant)
    visible = visible_branch_ids(user, tenant)
    on_date = on_date or tenant_today(tenant)
    levels = lessons.marking_levels(tenant)

    led, _ = placed_classes(active_classes(tenant, class_teacher__user=user), branches, visible)
    led.sort(key=lambda pair: (pair[0].level.order_index, pair[0].name.lower(), pair[0].pk))
    taught = []
    if levels:
        taught, _ = placed_classes(
            lessons.classes_taught(tenant, user, on_date), branches, visible,
        )

    every = {c.pk: (c, home) for c, home in led + taught}
    on_date, today_by_branch, calendar = resolve_day(tenant, list(every.values()), on_date, None)
    days = {pk: calendar.status(c, home, on_date) for pk, (c, home) in every.items()}
    open_classes = [c for c, _ in every.values() if days[c.pk].is_open]
    rosters = rosters_as_at(tenant, open_classes, on_date)
    registers = {
        r.school_class_id: r
        for r in ClassRegister.objects.filter(
            tenant=tenant, date=on_date, school_class__in=[c for c, _ in led],
        ).select_related("submitted_by")
    }

    cards = []
    for school_class, home in led:
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

    due = [(c, home) for c, home in taught if on_date <= today_by_branch[home.pk]]
    mine = lessons.with_state(
        tenant, lessons.day_lessons(tenant, due, days, on_date, levels, teacher=user),
        on_date, days, rosters,
    )
    return {
        "date": on_date,
        "date_label": format_date(on_date, tenant),
        "closed": whole_day_closed(led, days),
        "classes": cards,
        "lesson_marking_on": bool(levels),
        "lessons": [
            {
                "slot_id": lesson.slot.pk,
                "period_label": lesson.period_label,
                "start_time": lesson.starts_at,
                "subject": lesson.subject_name,
                "class_name": lesson.school_class.name,
                "status": lesson.status,
                "submitted_at": lesson.submitted_at,
            }
            for lesson in mine
        ],
    }
