"""Lessons: which a class has on a day, and one lesson's register as a document.

**What a lesson is.** A class has lessons to mark on a day when all of these hold:

* lesson marking is switched on for the class's level
  (``attendance.lesson_marking_levels``, by level code);
* the class's timetable is published;
* the school is open to the class that day (:class:`.days.DayCalendar`);
* the timetable has a slot for the day's weekday in a period that is a teaching
  period (a break, lunch or assembly is not a lesson) and still in use.

A lesson is identified by the class, the period and the date, which is what
:class:`~..models.LessonRegister` is unique over. The slot is the way in, not
the identity: saving a class's grid replaces its slots, so a register taken
before the save still belongs to the lesson its period and date name.

**A lesson register is read as one document**, the way a class register is
(:mod:`.registers`): the lesson, the day, the rows, the counts, who took it and
the history of changes. Each row carries the student's morning mark beside the
lesson mark. The morning mark is the class register's, and only a submitted
class register counts: a register still being filled in is a draft, and a row
whose class register is not yet in reads "not taken" (null) rather than quoting
a mark that may change.

**What the register says about the lesson is what it said when it was started.**
The period's label and times, the subject and the teacher are copied onto the
register by the first mark, so a later edit to the timetable never rewrites a
lesson that was already taken. Before any mark exists the lesson is described
from the timetable as it stands.

**The status is worked out on every read,** with the class register's words and
rules (:func:`.registers.status_of`): ``CLOSED_DAY``, ``SUBMITTED``,
``NOT_STARTED``, ``READY`` or ``IN_PROGRESS``. Locked rows (suspended children)
are kept out of every count.

**Bounded cost.** A list of the day's lessons reads the published timetables,
the slots, the lesson registers, the marks and the rosters once each for every
class in scope, whatever their number.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace

from rest_framework.exceptions import NotFound

from schools.vs_academics.models import SchoolClass, SessionStatus
from schools.vs_calendar.models import (
    ClassTimetable,
    DayOfWeek,
    PeriodType,
    PublishState,
    TimetableSlot,
)
from vs_config.display import format_date
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import CFG_LESSON_MARKING_LEVELS, Mark
from ..exceptions import LessonMarkingOff, LessonNotOnThisDay, TimetableNotPublished
from ..models import (
    ClassRegister,
    LessonMark,
    LessonMarkChange,
    LessonRegister,
    StudentMark,
)
from .access import open_class
from .days import DayStatus, school_day
from .registers import status_of, window_is_open
from schools.vs_students.services.roster import RosterEntry, roster_as_at, rosters_as_at
from .rules import read_attendance_rules
from .scope import CLASS_RELATIONS


def marking_levels(tenant) -> frozenset:
    """The level codes, lower-cased, whose classes mark lessons."""
    rules = read_attendance_rules(tenant, (CFG_LESSON_MARKING_LEVELS,))
    return frozenset(code.lower() for code in rules.lesson_marking_levels)


def level_marks_lessons(school_class, levels) -> bool:
    """Whether *school_class*'s level is one of the *levels* that mark lessons."""
    return school_class.level.code.lower() in levels


def is_lesson_slot(slot) -> bool:
    """Whether the slot sits in a teaching period that is still in use."""
    period = slot.period
    return period.period_type == PeriodType.LESSON and period.is_active


# ── The lessons of a day, many classes at once ─────────────────────────────

@dataclass
class DayLesson:
    """One lesson of one class on one date, with where its register stands."""

    slot: object
    school_class: object
    home: object
    register: object = None
    status: str = ""
    counts: dict = field(default_factory=dict)

    @property
    def submitted_at(self):
        return self.register.submitted_at if self.register is not None else None

    @property
    def submitted_by_name(self):
        register = self.register
        if register is None or not register.is_submitted or register.submitted_by is None:
            return None
        return register.submitted_by.full_name

    @property
    def period_label(self) -> str:
        return self.register.period_label if self.register is not None else self.slot.period.label

    @property
    def starts_at(self):
        return self.register.starts_at if self.register is not None else self.slot.period.start_time

    @property
    def ends_at(self):
        return self.register.ends_at if self.register is not None else self.slot.period.end_time

    @property
    def subject_name(self) -> str:
        subject = self.register.subject if self.register is not None else self.slot.subject
        return subject.name

    @property
    def teacher_name(self):
        teacher = self.register.teacher if self.register is not None else self.slot.teacher
        return teacher.full_name if teacher is not None else None


def day_lessons(tenant, pairs, days, on_date: dt.date, levels, *, teacher=None) -> list:
    """The lessons the classes in *pairs* have on *on_date*, by start time then class.

    *pairs* is ``[(class, branch)]`` and *days* is ``{class_id: DayStatus}``. A
    class is left out when the school is closed to it that day, its level does
    not mark lessons, or its timetable is not published. With *teacher*, only
    the slots that person is given.
    """
    eligible = {
        school_class.pk: (school_class, home)
        for school_class, home in pairs
        if days[school_class.pk].is_open and level_marks_lessons(school_class, levels)
    }
    if not eligible:
        return []
    published = set(
        ClassTimetable.objects.filter(
            tenant=tenant, school_class_id__in=list(eligible), status=PublishState.PUBLISHED,
        ).values_list("school_class_id", flat=True),
    )
    if not published:
        return []
    slots = TimetableSlot.objects.filter(
        tenant=tenant, school_class_id__in=published, day_of_week=on_date.isoweekday(),
        period__period_type=PeriodType.LESSON, period__is_active=True,
    )
    if teacher is not None:
        slots = slots.filter(teacher=teacher)
    lessons = [
        DayLesson(
            slot=slot, school_class=eligible[slot.school_class_id][0],
            home=eligible[slot.school_class_id][1],
        )
        for slot in slots.select_related("period", "subject", "teacher")
    ]
    lessons.sort(key=lambda lesson: (
        lesson.slot.period.start_time, lesson.school_class.name.lower(), lesson.slot.pk,
    ))
    return lessons


def classes_taught(tenant, user, on_date: dt.date) -> list:
    """The active classes in which *user* holds a lesson slot on *on_date*'s weekday."""
    taught = TimetableSlot.objects.filter(
        tenant=tenant, teacher=user, day_of_week=on_date.isoweekday(),
        period__period_type=PeriodType.LESSON, period__is_active=True,
    ).values("school_class_id")
    return list(
        SchoolClass.objects.filter(
            tenant=tenant, pk__in=taught, is_active=True,
            session__status=SessionStatus.ACTIVE,
        ).select_related(*CLASS_RELATIONS),
    )


def with_state(tenant, lessons, on_date: dt.date, days, rosters=None) -> list:
    """*lessons* with their register, status and counts filled in. Returns *lessons*.

    The rosters are the classes' rosters on the day, read here when not given.
    """
    if not lessons:
        return lessons
    classes = {lesson.school_class.pk: lesson.school_class for lesson in lessons}
    if rosters is None:
        rosters = rosters_as_at(tenant, list(classes.values()), on_date)
    registers = {
        (row.school_class_id, row.period_id): row
        for row in LessonRegister.objects.filter(
            tenant=tenant, date=on_date, school_class_id__in=list(classes),
        ).select_related("submitted_by", "subject", "teacher")
    }
    marked: dict[int, set] = {}
    for register_id, student_id in LessonMark.objects.filter(
        tenant=tenant, register_id__in=[row.pk for row in registers.values()],
    ).values_list("register_id", "student_id"):
        marked.setdefault(register_id, set()).add(student_id)

    for lesson in lessons:
        register = registers.get((lesson.school_class.pk, lesson.slot.period_id))
        lesson.register = register
        unlocked = {
            entry.student.pk for entry in rosters.get(lesson.school_class.pk, [])
            if not entry.suspended
        }
        done = len(unlocked & marked.get(register.pk, set())) if register is not None else 0
        lesson.counts = {"total": len(unlocked), "unmarked": len(unlocked) - done}
        lesson.status = status_of(
            SimpleNamespace(
                day=days[lesson.school_class.pk],
                is_submitted=register is not None and register.is_submitted,
            ),
            lesson.counts,
        )
    return lessons


# ── One lesson ─────────────────────────────────────────────────────────────

@dataclass
class LessonContext:
    """Everything a read or a write of one lesson register needs, resolved once."""

    tenant: object
    slot: object
    school_class: object
    branch: object
    date: dt.date
    day: DayStatus
    register: LessonRegister | None
    roster: list[RosterEntry] = field(default_factory=list)

    @property
    def is_submitted(self) -> bool:
        return self.register is not None and self.register.is_submitted

    @property
    def period(self):
        return self.slot.period

    @property
    def subject(self):
        return self.register.subject if self.register is not None else self.slot.subject

    @property
    def teacher(self):
        return self.register.teacher if self.register is not None else self.slot.teacher

    @property
    def period_label(self) -> str:
        return self.register.period_label if self.register is not None else self.period.label

    @property
    def starts_at(self):
        return self.register.starts_at if self.register is not None else self.period.start_time

    @property
    def ends_at(self):
        return self.register.ends_at if self.register is not None else self.period.end_time


def assert_lesson_applies(tenant, slot, school_class, on_date: dt.date) -> None:
    """Refuse a lesson that is not on that day, whose level is off or whose timetable is a draft."""
    if on_date.isoweekday() != slot.day_of_week:
        raise LessonNotOnThisDay(
            f"{slot.subject.name} for {school_class.name} runs on "
            f"{DayOfWeek(slot.day_of_week).label}s, not on {on_date:%A}s. Choose "
            f"a {DayOfWeek(slot.day_of_week).label}.",
        )
    if not is_lesson_slot(slot):
        raise LessonNotOnThisDay(
            f"{slot.period.label} is not a teaching period, so there is no lesson "
            f"to mark in it.",
        )
    if not level_marks_lessons(school_class, marking_levels(tenant)):
        raise LessonMarkingOff(
            f"Lesson marking is off for {school_class.level.name}. Switch it on "
            f"in Attendance settings to mark {school_class.name}'s lessons.",
        )
    published = ClassTimetable.objects.filter(
        tenant=tenant, school_class=school_class, status=PublishState.PUBLISHED,
    ).exists()
    if not published:
        raise TimetableNotPublished(
            f"{school_class.name}'s timetable is not published, so it has no "
            f"lessons to mark. Publish the timetable first.",
        )


def load_lesson(tenant, user, slot_id, on_date: dt.date) -> LessonContext:
    """Resolve the slot, the class and branch, the day, the register and the roster.

    Raises 404 for a slot that is not this school's and for a class the caller
    cannot open, and ``CLASS_HAS_NO_BRANCH`` for a shared class at a school with
    several branches. A closed day has no roster.
    """
    slot = (
        TimetableSlot.objects.filter(tenant=tenant, pk=slot_id)
        .select_related("period", "subject", "teacher").first()
    )
    if slot is None:
        raise NotFound("No such lesson at this school.")
    school_class, branch = open_class(tenant, user, slot.school_class_id)
    assert_lesson_applies(tenant, slot, school_class, on_date)
    day = school_day(tenant, school_class, branch, on_date)
    register = (
        LessonRegister.objects.filter(
            tenant=tenant, school_class=school_class, period_id=slot.period_id, date=on_date,
        ).select_related("submitted_by", "subject", "teacher").first()
    )
    roster = roster_as_at(tenant, school_class, on_date) if day.is_open else []
    return LessonContext(
        tenant=tenant, slot=slot, school_class=school_class, branch=branch,
        date=on_date, day=day, register=register, roster=roster,
    )


def _person(user) -> str:
    return user.full_name if user is not None else ""


def _morning_marks(ctx: LessonContext) -> dict:
    """``{student_id: mark}`` from the class's submitted register of the day, else empty."""
    register = ClassRegister.objects.filter(
        tenant=ctx.tenant, school_class=ctx.school_class, date=ctx.date,
        submitted_at__isnull=False,
    ).first()
    if register is None:
        return {}
    return dict(
        StudentMark.objects.filter(tenant=ctx.tenant, register=register)
        .values_list("student_id", "mark"),
    )


def _marks_by_student(ctx: LessonContext) -> dict:
    if ctx.register is None:
        return {}
    return {
        mark.student_id: mark
        for mark in LessonMark.objects.filter(tenant=ctx.tenant, register=ctx.register)
    }


def _row(entry: RosterEntry, mark, morning: dict) -> dict:
    locked = entry.suspended
    stored = None if locked else mark
    return {
        "student_id": entry.student.pk,
        "name": entry.student.full_name,
        "student_number": entry.student.student_number,
        "locked": locked,
        "mark": stored.mark if stored else None,
        "arrived_at": stored.arrived_at if stored else None,
        "note": stored.note if stored else "",
        "morning_mark": None if locked else morning.get(entry.student.pk),
    }


def _counts(rows) -> dict:
    live = [row for row in rows if not row["locked"]]
    tally = {code: 0 for code in (Mark.PRESENT, Mark.LATE, Mark.ABSENT)}
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
        "unmarked": unmarked,
        "locked": len(rows) - len(live),
    }


def _taken_by(ctx: LessonContext):
    register = ctx.register
    if register is None or not register.is_submitted:
        return None
    return {
        "submitted_by": _person(register.submitted_by),
        "submitted_at": register.submitted_at,
    }


def _history(ctx: LessonContext) -> list:
    if ctx.register is None:
        return []
    changes = (
        LessonMarkChange.objects.filter(tenant=ctx.tenant, mark__register=ctx.register)
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
            "note": change.note,
        }
        for change in changes
    ]


def lesson_payload(ctx: LessonContext) -> dict:
    """The body of a lesson register read, before Field Access is applied to it."""
    marks = _marks_by_student(ctx)
    morning = _morning_marks(ctx) if ctx.roster else {}
    rows = [_row(entry, marks.get(entry.student.pk), morning) for entry in ctx.roster]
    counts = _counts(rows)
    several = only_branch_id_or_several(ctx.tenant) is None
    return {
        "lesson": {
            "slot_id": ctx.slot.pk,
            "period_label": ctx.period_label,
            "start_time": ctx.starts_at,
            "end_time": ctx.ends_at,
            "subject": ctx.subject.name,
            "teacher_name": _person(ctx.teacher) or None,
        },
        "school_class": {
            "id": ctx.school_class.pk,
            "name": ctx.school_class.name,
            "branch": {"id": ctx.branch.pk, "name": ctx.branch.name} if several else None,
        },
        "date": ctx.date,
        "date_label": format_date(ctx.date, ctx.tenant, branch=ctx.branch),
        "closed": ctx.day.payload(),
        "status": status_of(ctx, counts),
        "roster_empty": ctx.day.is_open and not ctx.roster,
        "counts": counts,
        "taken_by": _taken_by(ctx),
        "edit_window_open": window_is_open(ctx) if ctx.is_submitted else None,
        "rows": rows,
        "history": _history(ctx),
    }

