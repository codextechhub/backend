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


class YearIsClosed(StudentsError):
    """A write aimed at a school year that has been archived.

    Carries ``vs_academics``' error code rather than one of its own. It is the
    same refusal about the same year, and a client that already knows how to
    render
    it should not have to learn a second name for it because the row being
    written belongs to a different module.
    """

    error_code = "SESSION_ARCHIVED_READ_ONLY"
    default_message = (
        "That school year is closed, so its roll can no longer be changed."
    )
    http_status = 409


class ClassBelongsToAnotherYear(StudentsError):
    """The class named is a different year's class of the same name.

    Reachable because a class belongs to a year. A school has one JSS1 A per
    year, all called JSS1 A, so an id from the wrong one looks entirely normal
    on every screen that renders the name.
    """

    error_code = "CLASS_BELONGS_TO_ANOTHER_YEAR"
    default_message = (
        "That class belongs to a different school year. Pick the class for "
        "the year this placement is in."
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


class ClassFull(StudentsError):
    """A full class at a school that does not put classes over capacity.

    The school's capacity rule is HARD, so unlike ``CLASS_AT_CAPACITY`` there
    is nothing the caller can send to go ahead: the refusal is the rule. A
    separate code rather than a flag on the other one, because a screen that
    offers "go ahead anyway" on ``CLASS_AT_CAPACITY`` must not offer it here.
    Raised by one child's placement, a bulk assignment and a promotion run;
    the last carries ``classes`` in the shape of the preview's
    ``over_capacity``.
    """

    error_code = "CLASS_FULL"
    default_message = (
        "That class is full, and this school does not put classes over "
        "capacity."
    )
    http_status = 422


class PromotionOverCapacity(StudentsError):
    """A promotion that would fill classes past their capacity, unacknowledged.

    The same rule as enrolling one child into a full class: refused until the
    caller says they mean it. ``classes`` lists each class, its capacity, the
    seats already taken and how many the run would add.
    """

    error_code = "PROMOTION_OVER_CAPACITY"
    default_message = (
        "This promotion would put classes over capacity. You can go ahead "
        "anyway."
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


class SuspensionEndsBeforeItStarts(StudentsError):
    """A suspension asked to end on or before the day it begins.

    A one-day suspension is a suspension that starts on Monday and ends on
    Tuesday, so the two dates are never the same day either. ``extra`` carries
    both dates, because the start is often the one that was mistyped and the
    screen has to say which pair it is refusing.
    """

    error_code = "SUSPENSION_ENDS_BEFORE_IT_STARTS"
    default_message = (
        "A suspension has to end after it starts. Pick a day the student comes "
        "back that falls after the day the suspension begins."
    )
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


class StudentSettingNotRegistered(StudentsError):
    """An enrolment rule's configuration definition is missing.

    The same failure as ``AdmissionPolicyNotRegistered`` for the enrolment
    rules, and refused for the same reason: storing nothing and answering
    success would leave a school believing a rule was set that every enrolment
    then ignores.
    """

    error_code = "STUDENT_SETTING_NOT_REGISTERED"
    default_message = (
        "The student settings are not registered on this platform yet. Run "
        "the migrations and seed_config_catalogue."
    )
    http_status = 500


class NotAnApplicant(StudentsError):
    """An admission stage move for a student who is no longer an applicant.

    A stage is where an application stands, so it has no meaning once the
    child is enrolled or the application is closed, and the stage a record
    ended at is kept as it was.
    """

    error_code = "NOT_AN_APPLICANT"
    default_message = "Only an applicant can be moved between admission stages."
    http_status = 422


class DocumentsMissing(StudentsError):
    """A child put on the roll without a document the school requires first.

    The school's own list (``applicants.documents.required_to_confirm``), held
    when an applicant is confirmed and when a child is enrolled directly. The
    message names the missing documents in words and ``missing`` lists them,
    so a screen can offer to attach each one.
    """

    error_code = "DOCUMENTS_MISSING"
    default_message = (
        "This school needs documents on an applicant's record before they "
        "can be confirmed."
    )
    http_status = 422


# ── moving a pupil to another branch ───────────────────────────────────────

class OneBranchSchool(StudentsError):
    """A school with one branch has nowhere to move a pupil to."""

    error_code = "ONE_BRANCH"
    default_message = (
        "This school has one branch, so there is no other branch to move a "
        "pupil to."
    )
    http_status = 409


class AlreadyAtBranch(StudentsError):
    error_code = "ALREADY_AT_BRANCH"
    default_message = "The pupil already attends that branch."
    http_status = 409


class NotOnRoll(StudentsError):
    """Only a pupil on the roll attends a branch, so only they can change one."""

    error_code = "NOT_ON_ROLL"
    default_message = "Only a pupil on the roll can move to another branch."
    http_status = 422


class BranchNotOpen(StudentsError):
    error_code = "BRANCH_NOT_OPEN"
    default_message = "That branch is not open, so no pupil can move into it."
    http_status = 422


class MoveClassRequired(StudentsError):
    """A placed pupil needs a class at the branch they move to.

    ACTIVE means placed and attending, so a placed pupil moved without a class
    would be active with no class at their new branch, which the roll has no
    shape for.
    """

    error_code = "CLASS_REQUIRED"
    default_message = "Choose the class the pupil joins at their new branch."
    http_status = 422


class MoveDateRefused(StudentsError):
    error_code = "INVALID_EFFECTIVE_DATE"
    default_message = "That date cannot be the day of the move."
    http_status = 422


class FinanceDidNotAnswer(StudentsError):
    """The books could not be reached, so the pupil's account cannot move with them.

    The move is refused rather than made without the account: a pupil at Lekki
    whose bills stay at Ikeja is the split the move exists to prevent.
    """

    error_code = "FINANCE_UNAVAILABLE"
    default_message = (
        "The school's books could not be reached, so nothing was moved. Try again "
        "in a few minutes."
    )
    http_status = 503
