"""The attendance report for one term, before Field Access is applied.

**What it covers.** One term, every active class of that term's school year in
the branches the caller can see (narrowed by a named branch), and only the days
up to today at the class's own branch. It is drawn from the registers that were
actually submitted: a day on which a class owed a register and did not submit
one is *not taken*, never an absence.

**The counting rules are the student's tab's, applied to the whole school.**
Present includes late; the rate is present over present plus absent plus
excused (:mod:`.rates`); a day the school was not open to the class counts
nowhere, whatever a register on it says; a suspended pupil, and a pupil not on
the roll that day, count nowhere. Who was on the roll, and suspended, is read as
at each day from the placements and the status log
(``vs_students.services.roster``), so a pupil
who moved class mid-term is judged against each class for its own stretch.

**The blocks.**

* ``range``: the first and last day covered and how many days the school was
  open to at least one class in scope;
* ``trend``: one row per week of the term that has begun, the week starting on
  the school's ``calendar.week_starts_on``, with the rate of the registers taken
  that week (``None`` when none was);
* ``by_class``: each class's figures, its not-taken days and its roll size;
* ``by_level``: the same added up by level name, because a ``Level`` row
  belongs to one branch and one year and the same year group has a row per
  branch;
* ``chronic``: the pupils whose term rate is below
  ``attendance.chronic_threshold``, lowest first. A pupil is listed only when
  they have at least one counted day and a rate strictly below the threshold, so
  a pupil exactly at the threshold is not listed. The rate compared is the one
  printed, so the list never shows a pupil at or above the line;
* ``not_taken``: the days a class owed a register and none was submitted,
  newest first;
* ``lessons_missed``: only when a class in scope is in a level that marks
  lessons, one row per subject, class and teacher with a submitted lesson
  register in the term: how many times a student was in school in the morning
  and absent from the lesson, and who (:mod:`.missed`). It sits beside the
  rest of the report and feeds none of it: no rate, count or chronic figure
  reads a lesson mark.

**Where the work is done.** The counting itself, one pass over the term's
calendar, rolls, registers and marks, is :func:`.term_summary.term_figures`,
shared with the system-side class summary and bounded in queries whatever the
size of the school. This module narrows it to the caller's classes and shapes
the blocks above.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from schools.vs_academics.models import SchoolClass
from schools.vs_students.models import Student
from vs_config.clock import tenant_today
from vs_rbac.scoping import visible_branch_ids

from ..constants import CFG_CHRONIC_THRESHOLD
from . import lessons, missed
from .rates import rate_percent
from .rules import read_attendance_rules
from .scope import placed_classes, school_branches
from .term_summary import counts_from_tally, empty_tally, term_figures
from .terms import chosen_term, term_block, terms_to_show


def _figures(tally: dict) -> dict:
    counts = counts_from_tally(tally)
    return {
        "present": counts.present,
        "late": counts.late,
        "absent": counts.absent,
        "excused": counts.excused,
        "rate": rate_percent(counts.present, counts.absent, counts.excused),
    }


def _empty_body(tenant, terms, term, today, threshold, several) -> dict:
    return {
        "term": term_block(term, today) if term is not None else None,
        "terms": [term_block(t, today) for t in terms],
        "range": {"start": None, "end": None, "school_days": 0},
        "trend": [],
        "by_class": [],
        "by_level": [],
        "chronic": [],
        "threshold": threshold,
        "not_taken": [],
        "branch_dimension": several,
    }


def _week_start(day: dt.date, starts_on: int) -> dt.date:
    """The first day of the week containing *day*, the week starting on ISO weekday *starts_on*."""
    return day - dt.timedelta(days=(day.isoweekday() - starts_on) % 7)


def build_report(tenant, user, *, term_id=None, branch=None, class_ids=None) -> dict:
    """The report for the term named by *term_id* (the default term when none is named).

    Raises 404 for a term that is not this school's. A caller sees only the
    classes of the branches they can see; *branch* narrows further and is
    resolved and checked by the view. *class_ids* narrows to those classes, so
    one class's figures are this report's arithmetic and never a second copy of
    it; whether the caller may ask about them is the view's to decide.
    """
    from schools.vs_calendar.constants import CFG_WEEK_STARTS_ON
    from schools.vs_calendar.services.calendar_rules import read_calendar_rules

    branches = school_branches(tenant)
    several = len(branches) > 1
    today = tenant_today(tenant)
    terms = terms_to_show(tenant, today)
    term = chosen_term(tenant, terms, term_id, today)
    threshold = read_attendance_rules(tenant, (CFG_CHRONIC_THRESHOLD,)).chronic_threshold
    body = _empty_body(tenant, terms, term, today, threshold, several)
    if term is None:
        return body

    classes = list(
        SchoolClass.objects.filter(tenant=tenant, is_active=True, session=term.session)
        .select_related("session", "level", "branch", "class_teacher", "class_teacher__user"),
    )
    placed, _ = placed_classes(classes, branches, visible_branch_ids(user, tenant), branch)
    if class_ids is not None:
        placed = [(c, home) for c, home in placed if c.pk in class_ids]
    figures = term_figures(tenant, term, placed)
    start, overall_end = figures.start, figures.end
    if overall_end < start:
        return body
    body["range"] = {"start": start, "end": overall_end, "school_days": 0}
    if not placed:
        return body

    states, registers = figures.states, figures.registers
    by_register, by_student = figures.by_register, figures.by_student
    not_taken_by_class, not_taken_rows = figures.not_taken_by_class, figures.not_taken_rows
    expected, latest_class = figures.expected, figures.latest_class

    by_class_tally = defaultdict(empty_tally)
    by_week = defaultdict(empty_tally)
    starts_on = read_calendar_rules(tenant, (CFG_WEEK_STARTS_ON,)).week_starts_on
    for register_id, tally in by_register.items():
        class_id, index, _ = registers[register_id]
        week = _week_start(start + dt.timedelta(days=index), starts_on)
        for code, count in tally.items():
            by_class_tally[class_id][code] += count
            by_week[week][code] += count

    body["range"]["school_days"] = sum(
        1 for index in range((overall_end - start).days + 1)
        if any(state.open_days[index] for state in states.values())
    )
    body["trend"] = _trend(start, overall_end, starts_on, by_week)
    body["by_class"], body["by_level"] = _by_class_and_level(
        states, by_class_tally, not_taken_by_class, several,
    )
    body["chronic"] = _chronic(
        tenant, by_student, expected, latest_class, threshold,
    )
    body["not_taken"] = _not_taken_rows(not_taken_rows, start, several)
    levels = lessons.marking_levels(tenant)
    marking = [c.pk for c, _ in placed if lessons.level_marks_lessons(c, levels)]
    if marking:
        body["lessons_missed"] = missed.lessons_missed_rows(
            tenant, start=start, end=overall_end, class_ids=marking, marking_codes=levels,
        )
    return body


def _trend(start, end, starts_on, by_week) -> list:
    rows, week, number = [], _week_start(start, starts_on), 1
    while week <= end:
        rows.append({
            "week_start": week, "label": f"Wk {number}",
            "rate": _figures(by_week[week])["rate"] if week in by_week else None,
        })
        week += dt.timedelta(days=7)
        number += 1
    return rows


def _by_class_and_level(states, tally_by_class, not_taken_by_class, several):
    classes, levels = [], {}
    ordered = sorted(
        (s for s in states.values() if s.ever_on_roll),
        key=lambda s: (
            s.home.name.lower(), s.school_class.level.order_index,
            s.school_class.name.lower(), s.school_class.pk,
        ),
    )
    for state in ordered:
        school_class, class_id = state.school_class, state.school_class.pk
        tally = tally_by_class.get(class_id) or empty_tally()
        counts = {
            **_figures(tally),
            "not_taken": not_taken_by_class.get(class_id, 0),
            "roster_size": state.roll_size,
        }
        classes.append({
            "class_id": class_id,
            "class_name": school_class.name,
            "level_name": school_class.level.name,
            "branch": {"id": state.home.pk, "name": state.home.name} if several else None,
            "counts": counts,
        })
        level = levels.setdefault(school_class.level.name, {
            "order": school_class.level.order_index, "tally": empty_tally(), "not_taken": 0,
        })
        level["order"] = min(level["order"], school_class.level.order_index)
        level["not_taken"] += counts["not_taken"]
        for code, count in tally.items():
            level["tally"][code] += count
    by_level = [
        {"level_name": name, **_figures(data["tally"]), "not_taken": data["not_taken"]}
        for name, data in sorted(levels.items(), key=lambda item: (item[1]["order"], item[0].lower()))
    ]
    return classes, by_level


def _chronic(tenant, by_student, expected, latest_class, threshold) -> list:
    below = {}
    for student_id, tally in by_student.items():
        figures = _figures(tally)
        if figures["rate"] is not None and figures["rate"] < threshold:
            below[student_id] = (tally, figures)
    if not below:
        return []
    students = {
        s.pk: s for s in Student.objects.filter(tenant=tenant, pk__in=list(below))
    }
    rows = []
    for student_id, (tally, figures) in below.items():
        student = students.get(student_id)
        if student is None:
            continue
        counted = sum(tally.values())
        rows.append({
            "student": {
                "id": student.pk, "name": student.full_name,
                "student_number": student.student_number,
            },
            "class_name": latest_class[student_id].name if student_id in latest_class else None,
            "rate": figures["rate"],
            "absent": figures["absent"],
            "excused": figures["excused"],
            "not_taken": max(0, expected.get(student_id, 0) - counted),
        })
    rows.sort(key=lambda row: (row["rate"], row["student"]["name"].lower(), row["student"]["id"]))
    return rows


def _not_taken_rows(cells, start, several) -> list:
    rows = []
    for index, state in cells:
        school_class = state.school_class
        teacher = school_class.class_teacher
        rows.append({
            "date": start + dt.timedelta(days=index),
            "class_id": school_class.pk,
            "class_name": school_class.name,
            "level_name": school_class.level.name,
            "class_teacher_name": teacher.user.full_name if teacher is not None else None,
            "branch": {"id": state.home.pk, "name": state.home.name} if several else None,
        })
    rows.sort(key=lambda row: (row["date"], row["class_name"].lower()), reverse=True)
    return rows
