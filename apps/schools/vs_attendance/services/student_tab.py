"""A student's Attendance tab: one term, day by day.

**The cell of a day.** Every teaching day of the term up to today, and every
weekday whether or not the school teaches it, is one cell, and a cell is exactly
one of:

* ``MARKED``: the pupil's class submitted a register and the pupil has a mark in
  it (``mark`` says which);
* ``NOT_TAKEN``: the school was open to the pupil's class and no submitted
  register has a mark for them;
* ``CLOSED``: the school was not open to the pupil's class (a weekend, a
  holiday, a day outside the term, a calendar entry for their branch, level or
  class), with the reason;
* ``SUSPENDED``: the pupil was on the roll and locked, and is counted nowhere;
* ``NOT_ON_ROLL``: the pupil had no placement in force that day, or had left the
  roll.

The cell is ``MARKED`` plus a ``mark`` rather than the mark itself, so a role
whose Field Access hides the mark still sees which days were open, closed or
missed, and the mark is the one field it loses.

**The class of a day is the class the pupil was placed in that day**, read from
their placements as of the date (``vs_students.services.roster.placement_in_force``)
and their status log, never the class they are in now. A pupil who moved from JSS1 A to
JSS1 B on the 20th has the first half of the term judged against JSS1 A's
registers and calendar and the second against JSS1 B's.
Every day the pupil was on the roll carries that class's ``class_id``, so a
screen can open the register the day belongs to; a day off the roll carries
none.

**The counts.** ``present`` includes late and ``late`` is the part of it that
arrived late. ``opened`` is the days the pupil was expected and the school was
open: present, absent, excused and not taken. The rate is present over present
plus absent plus excused, so days with no register are left out of it rather
than counted against the pupil (:mod:`.rates`).

**History.** Every change made in place to one of the pupil's marks within the
term, and every correction decided, approved or rejected. An approved
correction is one entry, the decision, and not also the mark change it caused.

**Lesson absences.** When lesson marking is on for the level of the pupil's class,
the tab also lists the lessons they were marked Absent from in the term
(``lesson_absences``, newest first), each with the pupil's morning mark beside
it. This is information beside the day cells and never inside them: a lesson
mark changes no count, no rate and no cell. The key is left out when the
pupil's level does not mark lessons.

**Bounded cost.** The calendar of the whole term is read once
(:class:`.days.DayCalendar`) and so are the pupil's placements, status log,
registers, marks, changes and corrections, so the queries do not grow with the
length of the term.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_students.constants import LEAVES_THE_ROLL, StudentStatus
from schools.vs_students.models import ClassEnrolment, Student, StudentStatusLog
from schools.vs_students.services.scoping import branch_dimension_applies, get_student_or_404
from vs_config.clock import branch_today

from ..constants import ChangeVia, CorrectionStatus, Mark
from ..models import ClassRegister, CorrectionRequest, MarkChange, StudentMark
from . import lessons, missed, notices
from .days import DayCalendar
from .rates import rate_percent
from schools.vs_students.services.roster import placement_in_force, status_on
from .term_summary import counts_from_tally, empty_tally
from .terms import chosen_term, term_block, terms_to_show

MARKED = "MARKED"
NOT_TAKEN = "NOT_TAKEN"
CLOSED = "CLOSED"
SUSPENDED = "SUSPENDED"
NOT_ON_ROLL = "NOT_ON_ROLL"


def _empty_counts() -> dict:
    return {
        "present": 0, "late": 0, "absent": 0, "excused": 0,
        "not_taken": 0, "opened": 0, "rate": None,
    }


def student_tab(tenant, user, student_id, term_id=None) -> dict:
    """The Attendance tab of one student, before Field Access is applied.

    Raises 404 for a student who is not this school's or sits in a branch the
    caller cannot see, and for a term that is not this school's.
    """
    student = get_student_or_404(
        tenant, user, student_id, queryset=Student.objects.select_related("branch"),
    )
    today = branch_today(tenant, student.branch)
    terms = terms_to_show(tenant, today)
    term = chosen_term(tenant, terms, term_id, today)

    guardian = notices.primary_guardians(tenant, [student.pk]).get(student.pk)
    body = {
        "student": {
            "id": student.pk,
            "name": student.full_name,
            "student_number": student.student_number,
            "class_name": None,
            "branch": (
                {"id": student.branch_id, "name": student.branch.name}
                if branch_dimension_applies(tenant) else None
            ),
        },
        "guardian": guardian.guardian if guardian is not None else None,
        "term": term_block(term, today) if term is not None else None,
        "terms": [term_block(t, today) for t in terms],
        "counts": _empty_counts(),
        "days": [],
        "absences": [],
        "history": [],
    }
    if term is None:
        return body

    end = min(term.end_date, today)
    enrolments = list(
        ClassEnrolment.objects.filter(tenant=tenant, student=student, session=term.session)
        .select_related("school_class", "school_class__branch", "school_class__level"),
    )
    current = _current_class(enrolments, min(today, term.end_date))
    body["student"]["class_name"] = current.name if current is not None else None
    marks_lessons = current is not None and lessons.level_marks_lessons(
        current, lessons.marking_levels(tenant),
    )
    if marks_lessons:
        body["lesson_absences"] = []
    if end < term.start_date:
        return body

    cells, marks = _cells(tenant, student, term, enrolments, end)
    body["days"] = cells
    body["counts"] = _counts(cells)
    body["absences"] = _absences(cells, marks)
    body["history"] = _history(tenant, student, term.start_date, end)
    if marks_lessons:
        body["lesson_absences"] = _lesson_absences(tenant, student, term.start_date, end)
    return body


def _current_class(enrolments, as_at: dt.date):
    """The class the pupil was placed in on *as_at*, or None."""
    current = placement_in_force(enrolments, as_at)
    return current.school_class if current is not None else None


def _cells(tenant, student, term, enrolments, end: dt.date):
    """``(cells, {date: mark})`` for every day of the term up to *end*."""
    calendar = DayCalendar(tenant, [term.session], term.start_date, end)
    logs = list(StudentStatusLog.objects.filter(
        tenant=tenant, student=student, effective_date__lte=end,
    ))
    class_ids = {row.school_class_id for row in enrolments}
    submitted = {
        (class_id, on_date): register_id
        for register_id, class_id, on_date in ClassRegister.objects.filter(
            tenant=tenant, school_class_id__in=class_ids,
            date__range=(term.start_date, end), submitted_at__isnull=False,
        ).values_list("pk", "school_class_id", "date")
    }
    marks_by_register = {
        mark.register_id: mark
        for mark in StudentMark.objects.filter(
            tenant=tenant, student=student, register_id__in=set(submitted.values()),
        ).select_related("reason_code")
    }

    cells, marks = [], {}
    day = term.start_date
    while day <= end:
        if day.isoweekday() in calendar.teaching_days or day.isoweekday() <= 5:
            cell = _cell(
                day, enrolments, logs, calendar, student, submitted, marks_by_register,
            )
            cells.append(cell)
            if "_mark" in cell:
                marks[day] = cell.pop("_mark")
        day += dt.timedelta(days=1)
    return cells, marks


def _cell(day, enrolments, logs, calendar, student, submitted, marks_by_register) -> dict:
    cell = {
        "date": day, "state": NOT_ON_ROLL, "class_id": None, "mark": None,
        "arrived_at": None, "closed_reason": None,
    }
    placement = placement_in_force(enrolments, day)
    status = status_on(logs, day)
    if placement is None or status in LEAVES_THE_ROLL:
        return cell

    school_class = placement.school_class
    cell["class_id"] = school_class.pk
    branch = school_class.branch or student.branch
    status_of_day = calendar.status(school_class, branch, day)
    if not status_of_day.is_open:
        cell["state"] = CLOSED
        cell["closed_reason"] = status_of_day.reason
        return cell
    if status == StudentStatus.SUSPENDED:
        cell["state"] = SUSPENDED
        return cell

    register_id = submitted.get((school_class.pk, day))
    mark = marks_by_register.get(register_id) if register_id is not None else None
    if mark is None:
        cell["state"] = NOT_TAKEN
        return cell
    cell.update({
        "state": MARKED, "mark": mark.mark,
        "arrived_at": mark.arrived_at, "_mark": (mark, school_class),
    })
    return cell


def _counts(cells) -> dict:
    tally = empty_tally()
    not_taken = 0
    for cell in cells:
        if cell["state"] == MARKED:
            tally[cell["mark"]] += 1
        elif cell["state"] == NOT_TAKEN:
            not_taken += 1
    counts = counts_from_tally(tally, not_taken)
    return {
        "present": counts.present,
        "late": counts.late,
        "absent": counts.absent,
        "excused": counts.excused,
        "not_taken": counts.not_taken,
        "opened": counts.opened,
        "rate": rate_percent(counts.present, counts.absent, counts.excused),
    }


def _absences(cells, marks) -> list:
    """The absent and excused days, newest first."""
    rows = []
    for cell in reversed(cells):
        if cell["state"] != MARKED or cell["mark"] not in (Mark.ABSENT, Mark.EXCUSED):
            continue
        mark, school_class = marks[cell["date"]]
        rows.append({
            "date": cell["date"],
            "class_name": school_class.name,
            "mark": mark.mark,
            "reason_code": mark.reason_code_id,
            "reason_label": mark.reason_code.name if mark.reason_code_id else None,
            "note": mark.note,
            "notice_state": mark.notice_state,
            "notice_sent_at": mark.notice_sent_at,
        })
    return rows


def _lesson_absences(tenant, student, start: dt.date, end: dt.date) -> list:
    """The lessons the pupil was marked Absent from, newest first.

    ``morning_mark`` is their mark in the class register of the same day, null
    when that register was not submitted.
    """
    rows = list(
        missed.lesson_absences(tenant, start=start, end=end, student=student)
        .select_related("register", "register__subject"),
    )
    rows.sort(key=lambda row: (row.register.date, row.register.starts_at), reverse=True)
    return [
        {
            "date": row.register.date,
            "period_label": row.register.period_label,
            "subject": row.register.subject.name,
            "morning_mark": row.morning,
        }
        for row in rows
    ]


def _history(tenant, student, start: dt.date, end: dt.date) -> list:
    """Changes made in place and corrections decided, newest first."""
    entries = []
    changes = (
        MarkChange.objects.filter(
            tenant=tenant, mark__student=student,
            mark__register__date__range=(start, end),
        )
        .exclude(via=ChangeVia.CORRECTION)
        .select_related("changed_by", "mark__register")
    )
    for change in changes:
        entries.append({
            "kind": "change",
            "at": change.changed_at,
            "date": change.mark.register.date,
            "from_mark": change.from_mark or None,
            "to_mark": change.to_mark,
            "changed_by_name": change.changed_by.full_name if change.changed_by_id else "",
            "via": change.via,
            "note": change.note,
        })
    decided = (
        CorrectionRequest.objects.filter(
            tenant=tenant, mark__student=student,
            mark__register__date__range=(start, end),
        )
        .exclude(status=CorrectionStatus.PENDING)
        .select_related("reason_code", "requested_by", "decided_by", "mark__register")
    )
    for correction in decided:
        entries.append({
            "kind": "correction",
            "at": correction.decided_at,
            "date": correction.mark.register.date,
            "from_mark": correction.from_mark,
            "to_mark": correction.to_mark,
            "arrived_at": correction.arrived_at,
            "status": correction.status,
            "via": ChangeVia.CORRECTION.value,
            "reason_code": correction.reason_code_id,
            "reason_label": correction.reason_code.name if correction.reason_code_id else None,
            "note": correction.note,
            "requested_by_name": (
                correction.requested_by.full_name if correction.requested_by_id else ""
            ),
            "requested_at": correction.requested_at,
            "decided_by_name": correction.decided_by.full_name if correction.decided_by_id else "",
            "decided_at": correction.decided_at,
            "decision_note": correction.decision_note,
        })
    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return entries
