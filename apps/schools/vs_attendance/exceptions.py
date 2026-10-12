"""Domain refusals, rendered by ``core.exceptions.custom_exception_handler``.

The handler reads exactly three attributes - ``error_code``, ``message`` and
``extra`` - and the status from ``http_status``, and drops anything else
silently, so a payload has to go in ``extra`` or it will not reach the caller.
Same contract as ``vs_calendar.exceptions``.

Every message is written for the person reading it on screen, because that is
where it lands: a refusal is shown verbatim under the control that caused it.
So each one says what to do next, not what the guard observed. A refusal about
one control carries that control's name as ``extra["field"]``.

Three answers are deliberately not declared here because the platform already
gives them: a class, a student or a register belonging to another school is a
plain 404 (never a 403, so identifiers cannot be enumerated), and a caller
without the permission key is the ordinary 403.
"""
from __future__ import annotations


class AttendanceError(Exception):
    error_code = "ATTENDANCE_ERROR"
    default_message = "The action could not be completed."
    http_status = 422

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


# ── The class and the day ──────────────────────────────────────────────────

class InvalidDate(AttendanceError):
    error_code = "INVALID_DATE"
    default_message = "That is not a date. Use the form 2026-01-12."
    http_status = 400


class ClassHasNoBranch(AttendanceError):
    """A class with no branch, at a school that has several.

    A register belongs to one branch: its notices name the branch, its closed
    days are the branch's, and a branch-bound reader sees only their own. A
    class shared across the school gives the register nowhere to live until a
    branch is chosen. At a school with one branch the question never arises,
    so the register is that branch's.
    """

    error_code = "CLASS_HAS_NO_BRANCH"
    default_message = "Give this class a branch before taking its register."


class DateInFuture(AttendanceError):
    error_code = "DATE_IN_FUTURE"
    default_message = (
        "That day has not come yet. A register can be taken only for today "
        "or an earlier day."
    )


class DayClosedNoRegister(AttendanceError):
    error_code = "DAY_CLOSED_NO_REGISTER"
    default_message = "The school is closed that day, so there is no register to take."


class EmptyRoster(AttendanceError):
    error_code = "EMPTY_ROSTER"
    default_message = (
        "Nobody is placed in this class on that day, so there is no register to take."
    )


# ── Who may take it, and until when ────────────────────────────────────────

class NotYourClass(AttendanceError):
    error_code = "NOT_YOUR_CLASS"
    default_message = (
        "You can take the register only for the class you lead. Ask an "
        "administrator to take this one for you."
    )
    http_status = 403


class OnlySubmitterMayChange(AttendanceError):
    error_code = "ONLY_SUBMITTER_MAY_CHANGE"
    default_message = (
        "This register has been submitted. Only the person who submitted it, "
        "or an administrator, can still change it today."
    )
    http_status = 403


class DayClosedUseCorrection(AttendanceError):
    error_code = "DAY_CLOSED_USE_CORRECTION"
    default_message = "This day is closed. Request a correction instead."


class AlreadySubmitted(AttendanceError):
    error_code = "ALREADY_SUBMITTED"
    default_message = "This register has already been submitted."
    http_status = 409


# ── The marks ──────────────────────────────────────────────────────────────

class StudentSuspended(AttendanceError):
    error_code = "STUDENT_SUSPENDED"
    default_message = (
        "This student was suspended on that day, so they cannot be marked."
    )


class ArrivalTimeRequired(AttendanceError):
    error_code = "ARRIVAL_TIME_REQUIRED"
    default_message = "Say when the student arrived."


class ReasonCodeRequired(AttendanceError):
    error_code = "REASON_CODE_REQUIRED"
    default_message = "Choose why the absence is excused."


class ReasonCodeRetired(AttendanceError):
    error_code = "REASON_CODE_RETIRED"
    default_message = "That reason is no longer offered. Choose another."


class NoteRequired(AttendanceError):
    error_code = "NOTE_REQUIRED"
    default_message = "Add a note saying what happened."


class UnmarkedRows(AttendanceError):
    error_code = "UNMARKED_ROWS"
    default_message = (
        "A register cannot be submitted with an unmarked row."
    )


# ── Reminders ──────────────────────────────────────────────────────────────

class AlreadyReminded(AttendanceError):
    error_code = "ALREADY_REMINDED"
    default_message = "The class teacher was already reminded about this register today."
    http_status = 409


class NoClassTeacher(AttendanceError):
    error_code = "NO_CLASS_TEACHER"
    default_message = "This class has no class teacher with a login to remind."


# ── Corrections ────────────────────────────────────────────────────────────

class CorrectionNotNeeded(AttendanceError):
    """A correction asked for while the register can still be changed directly."""

    error_code = "CORRECTION_NOT_NEEDED"
    default_message = "This day is still open. Change the mark on the register instead."


class NoChange(AttendanceError):
    error_code = "NO_CHANGE"
    default_message = "That is already the mark. Choose a different one."


class CorrectionAlreadyPending(AttendanceError):
    error_code = "CORRECTION_ALREADY_PENDING"
    default_message = (
        "A correction for this mark is already waiting for a decision. "
        "Wait for it to be decided before asking for another."
    )
    http_status = 409


class AlreadyDecided(AttendanceError):
    error_code = "ALREADY_DECIDED"
    default_message = "This correction has already been decided."
    http_status = 409


class SelfDecision(AttendanceError):
    error_code = "SELF_DECISION"
    default_message = (
        "You cannot decide a correction you asked for. Ask another administrator."
    )
    http_status = 403


class CorrectionStale(AttendanceError):
    """The mark moved on after the correction was asked for."""

    error_code = "CORRECTION_STALE"
    default_message = (
        "The mark has changed since this correction was asked for. Reject it "
        "and ask for a new one."
    )
    http_status = 409


# ── The school's settings ──────────────────────────────────────────────────

class DuplicateReasonCode(AttendanceError):
    error_code = "DUPLICATE_REASON_CODE"
    default_message = "There is already a reason with that name. Choose a different name."
    http_status = 409


class LastLiveReasonCode(AttendanceError):
    """Retiring the only reason still offered, which would leave nothing to excuse an absence with."""

    error_code = "LAST_LIVE_REASON_CODE"
    default_message = (
        "Keep at least one reason code in use, or no absence can be excused. "
        "Add another code before retiring this one."
    )


class UnknownLevelCodes(AttendanceError):
    """Lesson marking was switched on for a level the school does not have."""

    error_code = "UNKNOWN_LEVEL_CODE"
    default_message = "One of those levels does not exist at this school."
    http_status = 400


class AttendanceSettingNotRegistered(AttendanceError):
    """An attendance setting's configuration definition is missing.

    Refused rather than stored nowhere: answering success would leave a school
    believing its notices were off while every register kept sending them.
    """

    error_code = "ATTENDANCE_SETTING_NOT_REGISTERED"
    default_message = (
        "The attendance settings are not registered on this platform yet. Run "
        "the migrations and seed_config_catalogue."
    )
    http_status = 500


# ── The hubs ───────────────────────────────────────────────────────────────

class AdministratorsOnly(AttendanceError):
    error_code = "ADMINISTRATORS_ONLY"
    default_message = (
        "This is the school-wide view for administrators. Your own registers "
        "are at today/mine/."
    )
    http_status = 403
