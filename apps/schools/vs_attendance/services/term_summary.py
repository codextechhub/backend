"""One class's attendance counts over a term, for the modules that read them.

**What it is for.** Report cards and broadsheets in other modules print a
pupil's days present and absent and the class's days with no register. This
module answers both for one class and one term without a signed-in user:
:func:`class_term_summary` is a system read, so it applies no permission check
and no branch narrowing, which the calling module owns.

**One computation, three readers.** The counting rules live here and nowhere
else. The attendance report (:mod:`.report`) runs :func:`term_figures` over the
classes a caller can see; this module runs it over one class; the student's tab
(:mod:`.student_tab`) adds up its day cells with :func:`counts_from_tally`. They
agree by construction on what a day is:

* a day counts for a class only when the school was open to it
  (:class:`.days.DayCalendar`) and no later than that class's branch's today, so
  a future day is never "not taken";
* a pupil counts on a day only when they were on this class's roll that day, not
  suspended, read as at the day from their placements and status log
  (``vs_students.services.roster``), so a pupil who joined mid-term counts from
  their placement and one who moved class counts for each class over its own
  stretch;
* ``present`` includes ``late``; ``opened`` is present plus absent plus excused
  plus not taken; a day on which the class owed a register and none was
  submitted is not taken, for the class and for each pupil expected on it.

**Where the work is done.** Nothing is read per pupil or per day:

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
pupils for the segments. Both are bounded by one term of one school, so the
number of queries does not change with the size of a roll or the number of marks.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Count, Exists, OuterRef

from schools.vs_students.constants import LEAVES_THE_ROLL, StudentStatus
from schools.vs_students.models import ClassEnrolment, StudentStatusLog
from schools.vs_students.services.roster import placement_in_force, status_on
from vs_config.clock import branch_today, tenant_today
from vs_rbac.scoping import WHOLE_TENANT

from ..constants import Mark
from ..exceptions import ClassHasNoBranch
from ..models import ClassRegister, StudentMark
from .days import DayCalendar
from .scope import placed_classes, school_branches

_ONE_DAY = dt.timedelta(days=1)


@dataclass(frozen=True)
class StudentTermCounts:
    """One pupil's days in a term, counted the way the student's tab counts them.

    ``present`` includes ``late``, the part of it that arrived late. ``opened``
    is the days the pupil was expected and the school was open: present, absent,
    excused and not taken.
    """

    opened: int
    present: int
    absent: int
    late: int
    excused: int
    not_taken: int


@dataclass(frozen=True)
class ClassTermSummary:
    """A class's term: its days with no register, and each pupil's counts.

    ``students`` is keyed by student id and lists every pupil who was on the
    class's roll on at least one day counted.
    """

    days_not_taken: int
    students: dict


def empty_tally() -> dict:
    """A zero count for every mark."""
    return {code: 0 for code in Mark.values}


def counts_from_tally(tally: dict, not_taken: int = 0) -> StudentTermCounts:
    """The counts for a tally of marks and a number of days with no register."""
    present = tally[Mark.PRESENT] + tally[Mark.LATE]
    return StudentTermCounts(
        opened=present + tally[Mark.ABSENT] + tally[Mark.EXCUSED] + not_taken,
        present=present,
        absent=tally[Mark.ABSENT],
        late=tally[Mark.LATE],
        excused=tally[Mark.EXCUSED],
        not_taken=not_taken,
    )


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
        placement = placement_in_force(rows, first_day)
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


def _class_days(placed, calendar, tenant, term, start, overall_end, up_to=None):
    """``{class_id: _ClassDays}``: each class's open days up to its own branch's today.

    *up_to* can only shorten a class's days, never reach past its today.
    """
    states = {}
    total = (overall_end - start).days + 1
    for school_class, home in placed:
        last_day = min(term.end_date, branch_today(tenant, home))
        if up_to is not None:
            last_day = min(last_day, up_to)
        last_index = (last_day - start).days
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


@dataclass
class TermFigures:
    """Everything the readers of a term's registers share, for the classes in scope.

    ``states`` holds each class's open days and roll; ``expected`` is how many
    open days each pupil was expected on; ``latest_class`` is the class each
    counted pupil was last in; ``registers`` maps a register to its class, its
    day index and whether it was submitted; ``not_taken_rows`` and
    ``not_taken_by_class`` are the days a class owed a register and none was
    submitted; ``by_register`` and ``by_student`` are the tallies of counted
    marks.
    """

    start: dt.date
    end: dt.date
    states: dict = field(default_factory=dict)
    segments: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    latest_class: dict = field(default_factory=dict)
    registers: dict = field(default_factory=dict)
    not_taken_rows: list = field(default_factory=list)
    not_taken_by_class: dict = field(default_factory=dict)
    by_register: dict = field(default_factory=dict)
    by_student: dict = field(default_factory=dict)


def term_figures(tenant, term, placed, *, up_to: dt.date | None = None) -> TermFigures:
    """The counted figures of *term* for ``placed``, a list of ``(class, branch)``.

    Days run from the term's start to each class's branch's today or the term's
    end, whichever is earlier, and no further than *up_to* when it is given.
    ``end`` is the latest of those across the classes; it falls before the term
    started when none has begun, and the figures are then empty.
    """
    start = term.start_date
    latest_today = max(
        (branch_today(tenant, home) for _, home in placed), default=tenant_today(tenant),
    )
    overall_end = min(term.end_date, latest_today)
    if up_to is not None:
        overall_end = min(overall_end, up_to)
    figures = TermFigures(start=start, end=overall_end)
    if overall_end < start or not placed:
        return figures

    calendar = DayCalendar(tenant, [term.session], start, overall_end)
    states = _class_days(placed, calendar, tenant, term, start, overall_end, up_to)
    class_ids = list(states)
    segments, irregular = _load_segments(tenant, term, class_ids, start, overall_end)
    expected, latest_class = _place_on_rolls(segments, states, start)

    registers = {}
    for register_id, class_id, on_date, submitted_at in ClassRegister.objects.filter(
        tenant=tenant, school_class_id__in=class_ids, date__range=(start, overall_end),
    ).values_list("pk", "school_class_id", "date", "submitted_at"):
        registers[register_id] = (class_id, (on_date - start).days, submitted_at is not None)

    # A submitted register on a day the class was closed counts for nothing.
    taken, unusable = set(), []
    for register_id, (class_id, index, submitted) in registers.items():
        if not submitted:
            continue
        state = states[class_id]
        if 0 <= index <= state.last_index and state.open_days[index]:
            taken.add((class_id, index))
        else:
            unusable.append(register_id)

    # An open day with someone on the roll and no submitted register is not taken.
    not_taken_rows, not_taken_by_class = [], defaultdict(int)
    for class_id, state in states.items():
        running = 0
        for index in range(state.last_index + 1):
            running += state.roll[index]
            if state.open_days[index] and running > 0 and (class_id, index) not in taken:
                not_taken_by_class[class_id] += 1
                not_taken_rows.append((index, state))

    by_register, by_student = defaultdict(empty_tally), defaultdict(empty_tally)
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

    figures.states = states
    figures.segments = segments
    figures.expected = expected
    figures.latest_class = latest_class
    figures.registers = registers
    figures.not_taken_rows = not_taken_rows
    figures.not_taken_by_class = not_taken_by_class
    figures.by_register = by_register
    figures.by_student = by_student
    return figures


def class_term_summary(
    tenant, school_class, term, *, up_to: dt.date | None = None,
) -> ClassTermSummary:
    """The days *school_class* owed a register and took none, and each pupil's counts.

    A system read for the report cards and broadsheets of other modules: it
    takes no user and applies no permission check and no branch narrowing, so
    the caller decides whether its own reader may see the class. The figures are
    the attendance report's for this class and the student's tab's for each
    pupil who stayed in it, from one computation (:func:`term_figures`).

    Days run from the term's start to the earlier of the term's end and the
    class's branch's own today; *up_to* shortens that and never extends it, so a
    future day is never counted as a register not taken. A pupil's counts cover
    only the days they were on this class's roll, so a pupil who moved in or out
    of the class is counted for their stretch in it.

    Raises :class:`~..exceptions.ClassHasNoBranch` for a class with no branch at
    a school that has several, since its calendar has no branch to follow.
    """
    placed, unplaced = placed_classes([school_class], school_branches(tenant), WHOLE_TENANT)
    if unplaced:
        raise ClassHasNoBranch()
    figures = term_figures(tenant, term, placed, up_to=up_to)
    students = {}
    for student_id in figures.latest_class:
        counted = figures.by_student.get(student_id) or empty_tally()
        not_taken = max(0, figures.expected.get(student_id, 0) - sum(counted.values()))
        students[student_id] = counts_from_tally(counted, not_taken)
    return ClassTermSummary(
        days_not_taken=figures.not_taken_by_class.get(school_class.pk, 0),
        students=students,
    )
