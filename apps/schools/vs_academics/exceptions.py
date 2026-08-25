"""Domain refusals, rendered by ``core.exceptions.custom_exception_handler``.

The handler reads exactly three attributes - ``error_code``, ``message`` and
``extra`` - and drops anything else silently, so a payload has to go in
``extra`` or it will not reach the caller.

Every message here is written for the person reading it on screen, because that
is where it lands: the design renders a refusal verbatim under the control that
caused it. So each one says what the school still has to do, not what the guard
observed.
"""
from __future__ import annotations


class AcademicsError(Exception):
    error_code = "ACADEMICS_ERROR"
    default_message = "The academic action could not be completed."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class InvalidDateRange(AcademicsError):
    error_code = "INVALID_DATE_RANGE"
    default_message = "The end date must fall after the start date."
    http_status = 422


class TermOutsideSession(AcademicsError):
    error_code = "TERM_OUTSIDE_SESSION"
    default_message = "This term falls outside the session it belongs to."
    http_status = 422


class TermDatesOverlap(AcademicsError):
    error_code = "TERM_DATES_OVERLAP"
    default_message = "This term overlaps another term in the same session."
    http_status = 422


class TermOrderConflict(AcademicsError):
    error_code = "TERM_ORDER_CONFLICT"
    default_message = "The order of these terms disagrees with their dates."
    http_status = 422


class TermSessionNotDraft(AcademicsError):
    error_code = "TERM_SESSION_NOT_DRAFT"
    default_message = (
        "A term can only be deleted while its session is still a draft. "
        "Edit its dates instead."
    )
    http_status = 409


class SessionArchivedReadOnly(AcademicsError):
    error_code = "SESSION_ARCHIVED_READ_ONLY"
    default_message = (
        "This session is archived and cannot be changed. Make it the active "
        "session first if you need to edit it."
    )
    http_status = 409


class SessionHasArchivedTerm(AcademicsError):
    error_code = "SESSION_HAS_ARCHIVED_TERM"
    default_message = (
        "This session cannot be activated while it holds an archived term."
    )
    http_status = 409


class BranchScopeConflict(AcademicsError):
    error_code = "BRANCH_SCOPE_CONFLICT"
    default_message = (
        "This cannot belong to a wider part of the school than the item it "
        "sits inside."
    )
    http_status = 422


class DuplicateInBatch(AcademicsError):
    error_code = "DUPLICATE_IN_BATCH"
    default_message = "Some of these names already exist."
    http_status = 422


class LevelCycle(AcademicsError):
    error_code = "LEVEL_CYCLE"
    default_message = (
        "This would make the levels promote in a loop. Pick a level that comes "
        "later in the programme."
    )
    http_status = 422


class LevelCrossProgram(AcademicsError):
    error_code = "LEVEL_CROSS_PROGRAM"
    default_message = (
        "That level belongs to a different programme. Confirm the move if you "
        "meant it."
    )
    http_status = 422
