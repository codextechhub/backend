"""Domain refusals, rendered by ``core.exceptions.custom_exception_handler``.

The handler reads exactly three attributes - ``error_code``, ``message`` and
``extra`` - and drops anything else silently, so a payload has to go in
``extra`` or it will not reach the caller. Same contract as
``vs_academics.exceptions``.

Every message here is written for the person reading it on screen, because that
is where it lands: the design renders a refusal verbatim under the control that
caused it. So each one says what the school still has to do, not what the guard
observed.

Two refusals this module deliberately does **not** declare, because the platform
already answers them and a second code would make the same refusal answer
differently depending on which layer caught it:

* deleting a room, a period or an exam period that something points at is
  ``409 PROTECTED_REFERENCE``, from the ``PROTECT`` foreign key;
* a second slot for the same class, day and period is ``400 DUPLICATE``, from
  the unique constraint.
"""
from __future__ import annotations


class CalendarError(Exception):
    error_code = "CALENDAR_ERROR"
    default_message = "The action could not be completed."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


# ── Calendar events ────────────────────────────────────────────────────────

class InvalidDateRange(CalendarError):
    error_code = "INVALID_DATE_RANGE"
    default_message = "The end date cannot fall before the start date."
    http_status = 422


class EventOutsideSession(CalendarError):
    error_code = "EVENT_OUTSIDE_SESSION"
    default_message = "This date is outside the school year it belongs to."
    http_status = 422


class EventAudienceOutOfScope(CalendarError):
    """A level or class named in the audience is not the event's to narrow to.

    Either it belongs to another branch than the event does, or to another
    school year. Narrowing a Lekki event to an Ikeja class would produce an
    event nobody can see the reason for.
    """

    error_code = "EVENT_AUDIENCE_OUT_OF_SCOPE"
    default_message = (
        "That class is not part of this event's branch, so the event cannot be "
        "narrowed to it."
    )
    http_status = 422


# ── Bell schedule ──────────────────────────────────────────────────────────

class PeriodTimeInvalid(CalendarError):
    error_code = "PERIOD_TIME_INVALID"
    default_message = "The end time must be after the start time."
    http_status = 422


class PeriodOverlap(CalendarError):
    error_code = "PERIOD_OVERLAP"
    default_message = (
        "This overlaps another period on the same day and scope."
    )
    http_status = 422


class PeriodOrderConflict(CalendarError):
    """Kept although the API cannot reach it.

    The design's period form has label, start, end, type, applies-on and
    applies-to, and no order field, so ``order_index`` is assigned from the
    times and a caller cannot make it disagree with them. This stays for rows
    that arrive by import, by fixture or by migration - the same reason FR-007
    keeps two alerts M13 refuses at write time.
    """

    error_code = "PERIOD_ORDER_CONFLICT"
    default_message = "The order of these periods disagrees with their times."
    http_status = 422


# ── Class timetable ────────────────────────────────────────────────────────

class SlotPeriodNotTeaching(CalendarError):
    error_code = "SLOT_PERIOD_NOT_TEACHING"
    default_message = (
        "Lessons cannot be scheduled in a break, a lunch or an assembly."
    )
    http_status = 422


class SlotPeriodWrongDay(CalendarError):
    error_code = "SLOT_PERIOD_WRONG_DAY"
    default_message = "That period does not run on this day."
    http_status = 422


class NotATeachingUser(CalendarError):
    """Not a teacher at this school.

    The predicate is a role grant, not a persona column. ``user_type`` was
    dropped by ``vs_user`` migration 0009 and the FRD's section 4.8, which
    defines a teacher by it, no longer describes this platform. See
    ``services.teachers``.
    """

    error_code = "NOT_A_TEACHING_USER"
    default_message = (
        "That person is not a teacher at this school. Give them the teacher "
        "role first, then they can be put on a timetable."
    )
    http_status = 422


class RoomBranchConflict(CalendarError):
    error_code = "ROOM_BRANCH_CONFLICT"
    default_message = (
        "That room is at another branch, so this class cannot be scheduled "
        "into it."
    )
    http_status = 422


class TimetableSpansBranches(CalendarError):
    """One class's week may not be split across two branches.

    Not theoretical: a school-wide class is visible to both branches' admins
    under the inclusive read, so two of them can each start building JSS1 A's
    grid in their own rooms without either knowing about the other. A
    single-branch school can never reach this.
    """

    error_code = "TIMETABLE_SPANS_BRANCHES"
    default_message = (
        "This class already has lessons at another branch. A class's week "
        "happens at one branch, because the pupils cannot travel between "
        "periods."
    )
    http_status = 422


class RoomInUse(CalendarError):
    """A room holding lessons or papers cannot be deleted.

    Carries the platform's own ``PROTECTED_REFERENCE`` code rather than a new
    one, because the ``PROTECT`` foreign keys would raise the same refusal a
    layer down and the same refusal must not answer differently depending on
    which layer caught it. What this adds is the sentence: the generic detail
    names a constraint and this one names the way out, which is what a school
    reading it under a Delete button actually needs.
    """

    error_code = "PROTECTED_REFERENCE"
    default_message = (
        "This room already holds lessons. Deactivate it instead - it will stop "
        "appearing when anyone picks a room, and everything already scheduled "
        "here stays intact."
    )
    http_status = 409


class NoBellSchedule(CalendarError):
    error_code = "NO_BELL_SCHEDULE"
    default_message = (
        "A timetable grid is built on the school's periods, so the bell "
        "schedule has to come first."
    )
    http_status = 409


# ── Publication ────────────────────────────────────────────────────────────

class TimetableIncomplete(CalendarError):
    """Checked before clashes, because it is the more actionable message.

    Only reachable by duplicating another class's week without its teachers or
    rooms, which is a supported path that saves happily. Nothing else can write
    a slot with a gap in it.
    """

    error_code = "TIMETABLE_INCOMPLETE"
    default_message = (
        "Some lessons have no teacher or room yet. Fill them in and publish "
        "again."
    )
    http_status = 409


class TimetableHasClashes(CalendarError):
    error_code = "TIMETABLE_HAS_CLASHES"
    default_message = (
        "Some clashes are unresolved. Fix them and publish again."
    )
    http_status = 409


# ── Exams ──────────────────────────────────────────────────────────────────

class ExamEventNotExamPeriod(CalendarError):
    error_code = "EXAM_EVENT_NOT_EXAM_PERIOD"
    default_message = (
        "An exam timetable sits inside an exam period, so this calendar entry "
        "has to be one."
    )
    http_status = 422


class ExamOutsideExamPeriod(CalendarError):
    error_code = "EXAM_OUTSIDE_EXAM_PERIOD"
    default_message = "This date is outside the exam period."
    http_status = 422


class ExamPublishedReadOnly(CalendarError):
    error_code = "EXAM_PUBLISHED_READ_ONLY"
    default_message = (
        "This exam timetable has been published, so it can no longer be "
        "changed."
    )
    http_status = 409
