"""Domain refusals, rendered by ``core.exceptions.custom_exception_handler``.

The handler reads exactly three attributes - ``error_code``, ``message`` and
``extra`` - and the status from ``http_status``, and drops anything else
silently, so a payload has to go in ``extra`` or it will not reach the caller.
Same contract as ``vs_attendance.exceptions``.

Every message is written for the person reading it on screen, because that is
where it lands: a refusal is shown verbatim under the control that caused it.
So each one says what the caller still has to do, not what the guard observed.
A refusal about one control carries that control's name as ``extra["field"]``.

Three answers are deliberately not declared here because the platform already
gives them: a class, a sheet, a student, a structure or a scale belonging to
another school is a plain 404 (never a 403, so identifiers cannot be
enumerated), and a caller without the permission key is the ordinary 403.
"""
from __future__ import annotations


class GradebookError(Exception):
    error_code = "GRADEBOOK_ERROR"
    default_message = "The action could not be completed."
    http_status = 422

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


# ── Structures and scales in force ─────────────────────────────────────────

class NoStructure(GradebookError):
    error_code = "NO_STRUCTURE"
    default_message = (
        "This class's programme has no assessment structure in use. "
        "Set one up in Assessment setup."
    )
    http_status = 409


class NoScale(GradebookError):
    error_code = "NO_SCALE"
    default_message = (
        "This class's programme has no grading scale in use. "
        "Set one up in Assessment setup."
    )
    http_status = 409


class StructureTotal(GradebookError):
    """A structure that cannot be put in use: its maxima do not total 100, or it has two examinations."""

    error_code = "STRUCTURE_TOTAL"
    default_message = (
        "The components must add up to 100 marks and include at most one "
        "examination. Adjust the maximums, then try again."
    )


class ScaleGap(GradebookError):
    """A scale that cannot be put in use; the message is the reason the editor shows."""

    error_code = "SCALE_GAP"
    default_message = (
        "The bands must start at 0, give each band its own lowest mark and "
        "include at least one pass. Fix the bands, then try again."
    )


class RemarkBands(GradebookError):
    error_code = "REMARK_BANDS"
    default_message = (
        "Give each remark its own lowest average and include one that starts "
        "at 0, so every average has a remark."
    )


class NotADraft(GradebookError):
    error_code = "NOT_A_DRAFT"
    default_message = "Only a draft can be changed this way. Copy it to start a new draft."
    http_status = 409


class UsedByPublishedResult(GradebookError):
    error_code = "USED_BY_PUBLISHED_RESULT"
    default_message = (
        "A published result was made with this, so it can no longer be "
        "changed. Copy it to start a new draft and put that in use."
    )
    http_status = 409


class WouldInvalidateMarks(GradebookError):
    error_code = "WOULD_INVALIDATE_MARKS"
    default_message = (
        "Marks have already been entered under this structure, and this "
        "change would leave some of them invalid. Correct those marks first."
    )
    http_status = 409


class StructureChanged(GradebookError):
    """The class's columns were fixed by another sheet's first mark after this sheet was read."""

    error_code = "STRUCTURE_CHANGED"
    default_message = (
        "The assessment structure for this class changed while you were working. "
        "Reload the sheet and enter the marks again."
    )
    http_status = 409


class UnknownProgram(GradebookError):
    error_code = "UNKNOWN_PROGRAM"
    default_message = "Choose a programme from this school's list."
    http_status = 400


class UnknownBranch(GradebookError):
    error_code = "UNKNOWN_BRANCH"
    default_message = "Choose a branch from this school's list, or leave it blank for every branch."
    http_status = 400


class UnknownRow(GradebookError):
    """A row in an edit that names an id which is not one of the record's own."""

    error_code = "UNKNOWN_ROW"
    default_message = (
        "A row in this change is not part of this record any more. Reload it "
        "and make the change again."
    )
    http_status = 409


class UnknownProgramCodes(GradebookError):
    error_code = "UNKNOWN_PROGRAM_CODES"
    default_message = "Choose programme codes from the programmes this school has."
    http_status = 400


class GradebookSettingNotRegistered(GradebookError):
    error_code = "GRADEBOOK_SETTING_NOT_REGISTERED"
    default_message = "That setting is not available yet. Ask CodeX to enable it."
    http_status = 500


class ClassHasNoBranch(GradebookError):
    """A class with no branch, at a school that has several.

    Results belong to one branch: a branch-bound reader sees only their own
    branches' results and a report card names the branch it came from. A class
    shared across the school gives them nowhere to live until a branch is
    chosen. At a school with one branch the question never arises, so the
    results are that branch's.
    """

    error_code = "CLASS_HAS_NO_BRANCH"
    default_message = "Give this class a branch before entering its results."


class InvalidTerm(GradebookError):
    error_code = "INVALID_TERM"
    default_message = "Choose a term from this school's list."
    http_status = 400


# ── Who may act ────────────────────────────────────────────────────────────

class NotMainTeacher(GradebookError):
    error_code = "NOT_MAIN_TEACHER"
    default_message = (
        "Only the main teacher of this subject in this class can enter its "
        "scores. Ask an administrator to enter them for you."
    )
    http_status = 403


class NotHeadOfDepartment(GradebookError):
    error_code = "NOT_HEAD_OF_DEPARTMENT"
    default_message = (
        "Only the head of this subject's department can decide on it. Ask "
        "the head, or an administrator, to do it."
    )
    http_status = 403


class NotYourSheet(GradebookError):
    error_code = "NOT_YOUR_SHEET"
    default_message = (
        "You can open a score sheet only if you teach the subject in this "
        "class, lead the class or head the subject's department."
    )
    http_status = 403


class NotClassTeacher(GradebookError):
    error_code = "NOT_CLASS_TEACHER"
    default_message = (
        "Only the class teacher can compile and submit this class. Ask an "
        "administrator to do it for you."
    )
    http_status = 403


class NotYourClass(GradebookError):
    error_code = "NOT_YOUR_CLASS"
    default_message = (
        "You can open a class's results only if you are its class teacher or "
        "oversee results at the school."
    )
    http_status = 403


# ── Marks ──────────────────────────────────────────────────────────────────

class NotAWholeMark(GradebookError):
    error_code = "NOT_A_WHOLE_MARK"
    default_message = "Enter a whole number of marks, with no decimals."


class OverMax(GradebookError):
    error_code = "OVER_MAX"
    default_message = "That mark is above the maximum for this column."


class NegativeMark(GradebookError):
    error_code = "NEGATIVE_MARK"
    default_message = "A mark cannot be below 0."


class CellsRefused(GradebookError):
    """A batch of cells in which at least one was refused, so none was saved.

    ``extra["cells"]`` lists each refused cell as ``{student, component, code,
    message}``, so the screen can mark the cell and keep what was typed.
    """

    error_code = "CELLS_REFUSED"
    default_message = (
        "Some scores could not be saved, so none of this batch was. Correct "
        "the cells marked and save again."
    )


# ── The sheet and the class ────────────────────────────────────────────────

class BlanksRemain(GradebookError):
    """Cells are still blank; ``extra["blank_count"]`` says how many."""

    error_code = "BLANKS_REMAIN"
    default_message = "Fill every blank cell, using ABS or Excused where it applies, before submitting."


class NotSubmitted(GradebookError):
    error_code = "NOT_SUBMITTED"
    default_message = (
        "Only a sheet that has been submitted and is waiting for moderation "
        "can be approved or returned."
    )
    http_status = 409


class SheetLocked(GradebookError):
    error_code = "SHEET_LOCKED"
    default_message = (
        "This sheet has been submitted, so its scores can no longer be "
        "changed. Ask for it to be returned if a score is wrong."
    )
    http_status = 409


class ClassLocked(GradebookError):
    error_code = "CLASS_LOCKED"
    default_message = (
        "This class's results have gone to the principal, so scores can no "
        "longer be changed. Ask for the class to be returned if a score is wrong."
    )
    http_status = 409


class NoteRequired(GradebookError):
    error_code = "NOTE_REQUIRED"
    default_message = "Add a note saying why."


class SubjectsMissing(GradebookError):
    error_code = "SUBJECTS_MISSING"
    default_message = "Every subject's sheet must be in before the class can be submitted."
    http_status = 409


class CommentsMissing(GradebookError):
    error_code = "COMMENTS_MISSING"
    default_message = "Write a comment for every student before submitting the class."
    http_status = 409


class NotAwaiting(GradebookError):
    error_code = "NOT_AWAITING"
    default_message = "Only a class that is awaiting approval can be published or returned."
    http_status = 409


class NotPublished(GradebookError):
    error_code = "NOT_PUBLISHED"
    default_message = (
        "This class's results have not been published for this term yet. "
        "Report cards appear once the principal publishes the class."
    )
    http_status = 409


class AlreadyReleased(GradebookError):
    error_code = "ALREADY_RELEASED"
    default_message = "A released result cannot be held."
    http_status = 409


class AlreadyHeld(GradebookError):
    error_code = "ALREADY_HELD"
    default_message = "This student's result is already held."
    http_status = 409


class CommentRequired(GradebookError):
    error_code = "COMMENT_REQUIRED"
    default_message = "Write a comment before saving."


class InvalidRating(GradebookError):
    """A rating for a trait the school does not use, or a value outside 1 to 5."""

    error_code = "INVALID_RATING"
    default_message = "Rate each trait with a whole number from 1 to 5."


class UnknownBand(GradebookError):
    error_code = "UNKNOWN_BAND"
    default_message = (
        "Write comments only for the bands the school's remark templates list."
    )
    http_status = 400


class ReasonRequired(GradebookError):
    error_code = "REASON_REQUIRED"
    default_message = "Say why this result is being held."


class HoldNotOpen(GradebookError):
    error_code = "HOLD_NOT_OPEN"
    default_message = "This hold has already been lifted."
    http_status = 409


# ── Reminders ──────────────────────────────────────────────────────────────

class AlreadyReminded(GradebookError):
    error_code = "ALREADY_REMINDED"
    default_message = "The main teacher was already reminded about this sheet today."
    http_status = 409


class NoMainTeacher(GradebookError):
    error_code = "NO_MAIN_TEACHER"
    default_message = (
        "Nobody is assigned as the main teacher of this subject in this "
        "class. Assign one in Staff Management, then remind them."
    )


class TeacherUnreachable(GradebookError):
    error_code = "TEACHER_UNREACHABLE"
    default_message = "The main teacher cannot be reached. Contact them another way."
