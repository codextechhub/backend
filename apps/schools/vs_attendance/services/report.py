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
at each day from the placements and the status log (:mod:`.roster`), so a pupil
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

**Where the work is done.** Nothing is read per class or per day:

* the calendar of the term is read once (:class:`.days.DayCalendar`);
* the placements and status changes of every pupil who could be in a class in
  scope are read once and cut into *segments*, stretches of days over which the
  pupil's class and standing do not change;
* registers are read once, and marks are *grouped in the database* by register
  and by pupil, which is where the volume is (a school year's marks are
  pupils times days);
* a pupil with one placement and no status change ("regular", nearly every
  pupil) is judged by the database: a mark counts when the pupil was placed in
  the register's class by that date. Only the few pupils with a second placement
  or a status change ("irregular") have their marks read one by one and judged
  against their segments.

The Python loops that remain run over (class x day) for the calendar and over
pupils for the segments. Both are bounded by one term of one school, which is a
few thousand cells and a few thousand pupils whatever the number of marks, and
the number of queries does not change with either.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Count, Exists, OuterRef

from schools.vs_academics.models import SchoolClass
from schools.vs_students.constants import LEAVES_THE_ROLL, StudentStatus
from schools.vs_students.models import ClassEnrolment, Student, StudentStatusLog
from vs_config.clock import branch_today, tenant_today
from vs_rbac.scoping import visible_branch_ids

from ..constants import CFG_CHRONIC_THRESHOLD, Mark
from ..models import ClassRegister, StudentMark
from . import lessons, missed
from .days import DayCalendar
from .rates import rate_percent
from .roster import _placement_in_force, status_on
from .rules import read_attendance_rules
from .scope import placed_classes, school_branches
from .terms import chosen_term, term_block, terms_to_show

_ONE_DAY = dt.timedelta(days=1)


@dataclass
class _Segment:
    """A stretch of days (indices into the term) over which a pupil's class and standing are fixed."""

    first: int
    last: int
    class_id: int
    suspended: bool


@dataclass
class _ClassDays:
    """One class's calendar over the term and the people on its roll."""

    school_class: object
    home: object
    last_index: int
    open_days: list
    open_before: list = field(default_factory=list)
    roll: list = field(default_factory=list)
    ever_on_roll: bool = False
    roll_size: int = 0

    def open_between(self, first: int, last: int) -> int:
        """How many days from *first* to *last* the school was open to this class."""
        return self.open_before[last + 1] - self.open_before[first]


def _tally() -> dict:
    return {code: 0 for code in Mark.values}


def _figures(tally: dict) -> dict:
    present = tally[Mark.PRESENT] + tally[Mark.LATE]
    return {
        "present": present,
        "late": tally[Mark.LATE],
        "absent": tally[Mark.ABSENT],
        "excused": tally[Mark.EXCUSED],
        "rate": rate_percent(present, tally[Mark.ABSENT], tally[Mark.EXCUSED]),
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


def _segments(rows, logs, start: dt.date, end: dt.date) -> list:
    """The stretches of days between *start* and *end* over which one pupil's standing is fixed.

    The class a pupil is in changes on the effective date of a placement, and
    whether they are on the roll or suspended changes on the effective date of
    a status change and on the return date of a suspension, so those are the
    only days a new stretch can begin on. A stretch with no placement in force,
    or with the pupil off the roll, is left out.
    """
    cuts = {start}
    for row in rows:
        if start < row.effective_date <= end:
            cuts.add(row.effective_date)
    for log in logs:
        for day in (log.effective_date, log.return_date):
            if day is not None and start < day <= end:
                cuts.add(day)
    points = sorted(cuts)
    out = []
    for position, first_day in enumerate(points):
        last_day = points[position + 1] - _ONE_DAY if position + 1 < len(points) else end
        placement = _placement_in_force(rows, first_day)
        if placement is None:
            continue
        status = status_on(logs, first_day)
        if status in LEAVES_THE_ROLL:
            continue
        out.append(_Segment(
            first=(first_day - start).days, last=(last_day - start).days,
            class_id=placement.school_class_id,
            suspended=status == StudentStatus.SUSPENDED,
        ))
    return out


def _week_start(day: dt.date, starts_on: int) -> dt.date:
    """The first day of the week containing *day*, the week starting on ISO weekday *starts_on*."""
    return day - dt.timedelta(days=(day.isoweekday() - starts_on) % 7)


def _class_days(placed, calendar, tenant, term, start, overall_end):
    """``{class_id: _ClassDays}``: each class's open days up to its own branch's today."""
    states = {}
    total = (overall_end - start).days + 1
    for school_class, home in placed:
        last_index = (min(term.end_date, branch_today(tenant, home)) - start).days
        open_days = [False] * total
        for index in range(min(last_index, total - 1) + 1):
            open_days[index] = calendar.status(
                school_class, home, start + dt.timedelta(days=index),
            ).is_open
        before = [0]
        for flag in open_days:
            before.append(before[-1] + (1 if flag else 0))
        states[school_class.pk] = _ClassDays(
            school_class=school_class, home=home, last_index=last_index,
            open_days=open_days, open_before=before, roll=[0] * (total + 1),
        )
    return states


def _load_segments(tenant, term, class_ids, start, end):
    """``(segments, irregular)``: every candidate pupil's stretches, and who has more than one.

    A candidate is a pupil placed in one of the classes by the last day. A
    pupil is irregular when they have a second placement or any status change,
    which is when a mark cannot be judged by the database alone.
    """
    candidates = ClassEnrolment.objects.filter(
        tenant=tenant, school_class_id__in=class_ids, effective_date__lte=end,
    ).values("student_id")
    placements = defaultdict(list)
    for row in ClassEnrolment.objects.filter(
        tenant=tenant, session=term.session, student_id__in=candidates,
    ):
        placements[row.student_id].append(row)
    logs = defaultdict(list)
    for log in StudentStatusLog.objects.filter(
        tenant=tenant, student_id__in=candidates, effective_date__lte=end,
    ):
        logs[log.student_id].append(log)

    segments, irregular = {}, set()
    for student_id, rows in placements.items():
        student_logs = logs.get(student_id, [])
        if len(rows) > 1 or student_logs:
            irregular.add(student_id)
        segments[student_id] = _segments(rows, student_logs, start, end)
    return segments, irregular


def _place_on_rolls(segments, states, start):
    """Fill each class's roll and each pupil's expected days from their segments.

    Returns ``(expected, latest_class)``: how many open days each pupil was
    expected on, and the class they were last in within scope.
    """
    expected = defaultdict(int)
    latest = {}
    for student_id, stretches in segments.items():
        for segment in stretches:
            state = states.get(segment.class_id)
            if state is None:
                continue
            last = min(segment.last, state.last_index)
            if last < segment.first:
                continue
            state.roll[segment.first] += 1
            state.roll[last + 1] -= 1
            state.ever_on_roll = True
            latest[student_id] = state.school_class
            if segment.suspended:
                continue
            expected[student_id] += state.open_between(segment.first, last)
            if segment.first <= state.last_index <= segment.last:
                state.roll_size += 1
    return expected, latest


def _counted_in_segments(stretches, class_id, index) -> bool:
    """Whether a pupil was on the roll of *class_id*, not suspended, on day *index*."""
    return any(
        s.class_id == class_id and s.first <= index <= s.last and not s.suspended
        for s in stretches
    )


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
    start = term.start_date
    latest_today = max(
        (branch_today(tenant, home) for _, home in placed), default=today,
    )
    overall_end = min(term.end_date, latest_today)
    if overall_end < start:
        return body
    body["range"] = {"start": start, "end": overall_end, "school_days": 0}
    if not placed:
        return body

    calendar = DayCalendar(tenant, [term.session], start, overall_end)
    states = _class_days(placed, calendar, tenant, term, start, overall_end)
    class_ids = list(states)
    segments, irregular = _load_segments(tenant, term, class_ids, start, overall_end)
    expected, latest_class = _place_on_rolls(segments, states, start)

    registers = {}
    for register_id, class_id, on_date, submitted_at in ClassRegister.objects.filter(
        tenant=tenant, school_class_id__in=class_ids, date__range=(start, overall_end),
    ).values_list("pk", "school_class_id", "date", "submitted_at"):
        registers[register_id] = (class_id, (on_date - start).days, submitted_at is not None)

    taken, unusable = set(), []
    for register_id, (class_id, index, submitted) in registers.items():
        if not submitted:
            continue
        state = states[class_id]
        if 0 <= index <= state.last_index and state.open_days[index]:
            taken.add((class_id, index))
        else:
            unusable.append(register_id)

    not_taken_rows, not_taken_by_class = [], defaultdict(int)
    for class_id, state in states.items():
        running = 0
        for index in range(state.last_index + 1):
            running += state.roll[index]
            if state.open_days[index] and running > 0 and (class_id, index) not in taken:
                not_taken_by_class[class_id] += 1
                not_taken_rows.append((index, state))

    by_register, by_student = defaultdict(_tally), defaultdict(_tally)
    marks = StudentMark.objects.filter(
        tenant=tenant, register__school_class_id__in=class_ids,
        register__date__range=(start, overall_end), register__submitted_at__isnull=False,
    ).exclude(register_id__in=unusable)
    placed_in_class = ClassEnrolment.objects.filter(
        tenant=tenant, student_id=OuterRef("student_id"),
        school_class_id=OuterRef("register__school_class_id"),
        effective_date__lte=OuterRef("register__date"),
    )
    regular = marks.exclude(student_id__in=irregular).filter(Exists(placed_in_class))
    for register_id, mark, count in regular.values_list("register_id", "mark").annotate(
        n=Count("pk"),
    ).order_by():
        by_register[register_id][mark] += count
    for student_id, mark, count in regular.values_list("student_id", "mark").annotate(
        n=Count("pk"),
    ).order_by():
        by_student[student_id][mark] += count
    if irregular:
        for register_id, student_id, mark in marks.filter(
            student_id__in=irregular,
        ).values_list("register_id", "student_id", "mark"):
            class_id, index, _ = registers[register_id]
            if _counted_in_segments(segments.get(student_id, ()), class_id, index):
                by_register[register_id][mark] += 1
                by_student[student_id][mark] += 1

    by_class_tally = defaultdict(_tally)
    by_week = defaultdict(_tally)
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
        tally = tally_by_class.get(class_id) or _tally()
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
            "order": school_class.level.order_index, "tally": _tally(), "not_taken": 0,
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
