"""Domain refusals, rendered by ``core.exceptions.custom_exception_handler``.

The handler reads exactly three attributes - ``error_code``, ``message`` and
``extra`` - and drops anything else, so a payload goes in ``extra`` or it never
reaches the caller.

Every message is written for the person reading it, because the design renders
a refusal verbatim under the control that caused it. Each one says what the
school still has to do rather than what the guard observed.

FRD M11 v2.4 section 11.
"""
from __future__ import annotations


class StudentsError(Exception):
    error_code = "STUDENTS_ERROR"
    default_message = "That could not be done to this student record."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class DuplicateStudent(StudentsError):
    """Same name, same birthday, same school. Advisory, not final."""

    error_code = "DUPLICATE_STUDENT"
    default_message = (
        "A student with this name and date of birth is already on the roll. "
        "Confirm that this is a different child to continue."
    )
    http_status = 409


class DuplicateStudentNumber(StudentsError):
    error_code = "DUPLICATE_STUDENT_NUMBER"
    default_message = "Another student at this school already holds that number."
    http_status = 409


class AdmissionNumberRequired(StudentsError):
    error_code = "ADMISSION_NUMBER_REQUIRED"
    default_message = "This school requires an admission number for every student."
    http_status = 422


class AdmissionNumberFormat(StudentsError):
    """The message quotes the school's hint, never the pattern.

    "Use the BFS/YYYY/NNNN format." is a sentence a registrar can act on;
    ``^BFS/\\d{4}/\\d{4}$`` is not.
    """

    error_code = "ADMISSION_NUMBER_FORMAT"
    default_message = "That admission number is not in this school's format."
    http_status = 422


class ClassAtCapacity(StudentsError):
    error_code = "CLASS_AT_CAPACITY"
    default_message = (
        "That class is full. You can go ahead anyway, which will put it over "
        "capacity."
    )
    http_status = 422


class GuardianRequired(StudentsError):
    error_code = "GUARDIAN_REQUIRED"
    default_message = "Every student needs at least one guardian."
    http_status = 422


class PrimaryGuardianRequired(StudentsError):
    error_code = "PRIMARY_GUARDIAN_REQUIRED"
    default_message = "Exactly one guardian must be the primary contact."
    http_status = 422


class NoActiveSession(StudentsError):
    error_code = "NO_ACTIVE_SESSION"
    default_message = (
        "This school has no active session, so there is nothing to place a "
        "student into. Activate a session in Academic Structure first."
    )
    http_status = 422


class ReasonRequired(StudentsError):
    error_code = "REASON_REQUIRED"
    default_message = "Give a reason. It goes into the student's history."
    http_status = 422


class DestinationRequired(StudentsError):
    error_code = "DESTINATION_REQUIRED"
    default_message = "Say which school the student is transferring to."
    http_status = 422


class PlacementRequired(StudentsError):
    error_code = "PLACEMENT_REQUIRED"
    default_message = (
        "A returning student needs a class. Their old one may have been "
        "archived or belong to a session that has ended."
    )
    http_status = 422


class InvalidStatusTransition(StudentsError):
    error_code = "INVALID_STATUS_TRANSITION"
    default_message = "That status change is not allowed from where this student is."
    http_status = 422


class BranchChangeNotSupported(StudentsError):
    error_code = "BRANCH_CHANGE_NOT_SUPPORTED"
    default_message = (
        "A student cannot be moved to another branch by editing their record."
    )
    http_status = 422


class BranchScopeConflict(StudentsError):
    """The one refusal with no override.

    Deliberately a different answer from the 404 a class the caller cannot see
    gets: a class they cannot see does not exist as far as they are concerned,
    while a class they can see but this child may not join is a rule they are
    entitled to be told about.
    """

    error_code = "BRANCH_SCOPE_CONFLICT"
    default_message = "That class belongs to another branch."
    http_status = 422


class NothingToMove(StudentsError):
    """Opening Transfer on a student who has no class.

    A sentence rather than a form, which is what the design shows.
    """

    error_code = "NOTHING_TO_MOVE"
    default_message = "This student is not in a class, so there is nothing to move."
    http_status = 422


class TerminalStatus(StudentsError):
    error_code = "TERMINAL_STATUS"
    default_message = "This is a final status. There is nothing to move the student to."
    http_status = 422


class BulkTooLarge(StudentsError):
    error_code = "BULK_TOO_LARGE"
    default_message = "Too many students in one action. Select fewer and try again."
    http_status = 422


class InvalidAdmissionPattern(StudentsError):
    error_code = "INVALID_ADMISSION_PATTERN"
    default_message = "That admission number pattern is not a valid expression."
    http_status = 422


class AdmissionPolicyNotRegistered(StudentsError):
    """The configuration definitions have not been seeded on this platform.

    Deliberately NOT the same code as an uncompilable pattern. Sharing one made
    a test asserting the pattern refusal pass on a database where nothing was
    registered at all, so the rule looked enforced and was not.
    """

    error_code = "ADMISSION_POLICY_NOT_REGISTERED"
    default_message = (
        "The admission number settings are not registered on this platform "
        "yet. Run seed_config_catalogue."
    )
    http_status = 500
