"""Names this module's views, services, serializers and seeder must agree on.

Kept in one place so a typo cannot make a view demand a key the seeder never
registers, which fails as a 403 nobody can act on rather than as an error. Same
arrangement as ``vs_attendance.constants``.
"""
from __future__ import annotations

from django.db import models

# ── Permission keys ────────────────────────────────────────────────────────
# Registered, grouped and granted by
# ``core.management.commands.seed_school_permissions``.
#
# A key answers whether somebody may open a screen or take an action at all.
# Which sheet or class they may act on is a domain rule in the services:
# ``sheet.enter`` is the main teacher's own sheet, ``sheet.enter_any`` is any
# sheet in the branches the holder can see, ``moderation.approve`` is the head
# of the subject's department, ``class.submit`` is the class teacher.
PERM_HUB_VIEW = "gradebook.hub.view"
PERM_SHEET_VIEW = "gradebook.sheet.view"
PERM_SHEET_ENTER = "gradebook.sheet.enter"
PERM_SHEET_ENTER_ANY = "gradebook.sheet.enter_any"
PERM_SHEET_REMIND = "gradebook.sheet.remind"
PERM_MODERATION_APPROVE = "gradebook.moderation.approve"
PERM_CLASS_SUBMIT = "gradebook.class.submit"
PERM_CLASS_PUBLISH = "gradebook.class.publish"
PERM_HOLD_VIEW = "gradebook.hold.view"
PERM_HOLD_UPDATE = "gradebook.hold.update"
PERM_RELEASE_VIEW = "gradebook.release.view"
PERM_RESULT_VIEW = "gradebook.result.view"
PERM_SETUP_VIEW = "gradebook.setup.view"
PERM_SETUP_UPDATE = "gradebook.setup.update"
PERM_AMENDMENT_CREATE = "gradebook.amendment.create"
PERM_AMENDMENT_APPROVE = "gradebook.amendment.approve"
PERM_SHEET_IMPORT = "gradebook.sheet.import"
PERM_ANALYTICS_VIEW = "gradebook.analytics.view"
PERM_ANALYTICS_EXPORT = "gradebook.analytics.export"

#: The key that unlocks a staff account. Holding it is what lets a late-sheet
#: row say that its teacher cannot sign in.
PERM_SEE_LOCKED_ACCOUNTS = "school.administrators.reactivate"

#: The resources whose registered fields Field Access governs.
SHEET_FIELD_RESOURCE = "gradebook.sheet"
RESULT_FIELD_RESOURCE = "gradebook.result"
HOLD_FIELD_RESOURCE = "gradebook.hold"
SETUP_FIELD_RESOURCE = "gradebook.setup"
AMENDMENT_FIELD_RESOURCE = "gradebook.amendment"

#: The switch a cell's mark is read and written through, wherever a mark is printed.
MARK_FIELD = "gradebook.sheet.mark"

# ── The school's gradebook settings ────────────────────────────────────────
# Six school-scoped ``vs_config`` definitions, declared by migration
# ``0001_initial`` and by ``seed_config_catalogue``, read in ``services.rules``.
CFG_SHOW_CLASS_STATS = "gradebook.show_class_stats"
CFG_RANK_IN_CLASS = "gradebook.rank_in_class"
CFG_CONDUCT_RATINGS = "gradebook.conduct_ratings"
CFG_RATING_KEY = "gradebook.rating_key"
CFG_RATING_TRAITS = "gradebook.rating_traits"
CFG_MODERATED_PROGRAMS = "gradebook.moderated_programs"

DEFAULT_SHOW_CLASS_STATS = True
DEFAULT_RANK_IN_CLASS = True
DEFAULT_CONDUCT_RATINGS = True
DEFAULT_RATING_KEY = True

#: The traits a class teacher rates, in the order the report card prints them.
DEFAULT_RATING_TRAITS = (
    "Punctuality",
    "Neatness",
    "Politeness",
    "Honesty",
    "Relationship with others",
    "Attentiveness",
    "Handwriting",
    "Sports",
    "Drawing and painting",
    "Handling of tools",
)
RATING_TRAITS_MIN = 1
RATING_TRAITS_MAX = 15
RATING_TRAIT_NAME_MAX = 60

#: A rating is a whole number on this scale.
RATING_MIN = 1
RATING_MAX = 5

# ── Assessment structures and grading scales ───────────────────────────────

#: What the components of a structure must add up to.
STRUCTURE_TOTAL_MARKS = 100
COMPONENT_MAX_SCORE_MIN = 1
COMPONENT_MAX_SCORE_MAX = 100
COMPONENT_NAME_MAX = 60
COMPONENT_SHORT_NAME_MAX = 12
COMPONENTS_MAX = 12

SETUP_NAME_MAX = 80
BAND_GRADE_MAX = 10
BAND_REMARK_MAX = 120
BAND_MIN_SCORE_MIN = 0
BAND_MIN_SCORE_MAX = 100
BANDS_MAX = 20

REMARK_TEXT_MAX = 300

#: The principal's remark templates a school starts with, as
#: ``(lowest overall average, sentence)``.
DEFAULT_REMARK_TEMPLATES = (
    (70, "An excellent result. Keep it up."),
    (55, "A good result. A little more effort will show."),
    (40, "A fair result. More consistent work is needed."),
    (0, "This result needs attention. Let us meet before next term."),
)
REMARK_TEMPLATES_MAX = 10

# ── Notification events ────────────────────────────────────────────────────
EVENT_SHEET_REMINDER = "gradebook.sheet_reminder"
EVENT_SHEET_RETURNED = "gradebook.sheet_returned"
EVENT_CLASS_RETURNED = "gradebook.class_returned"
EVENT_REPORT_CARD_READY = "gradebook.report_card_ready"
#: To the pupil, when their own result is released: that it is ready to read.
EVENT_RESULT_RELEASED = "gradebook.result_released"
EVENT_REPORT_CARD_UPDATED = "gradebook.report_card_updated"


# ── Sheets ─────────────────────────────────────────────────────────────────

#: The refusal code of one cell in a batch write, beside ``NOT_A_WHOLE_MARK``,
#: ``OVER_MAX`` and ``NEGATIVE_MARK`` which have exceptions of their own.
CELL_NOT_ON_SHEET = "STUDENT_NOT_ON_SHEET"
CELL_UNKNOWN_COMPONENT = "UNKNOWN_COMPONENT"
CELL_MARK_AND_MARKER = "MARK_AND_MARKER"
CELL_BAD_MARKER = "INVALID_MARKER"
CELL_DUPLICATE = "DUPLICATE_CELL"

#: The refusal code of a cell an amendment leaves blank: a published result has
#: a mark, ABS or Excused in every column.
CELL_BLANK = "BLANK_CELL"

#: A batch write carries at most this many cells (sixty students, twelve components).
CELLS_MAX = 1000

# ── Bulk upload of a sheet ─────────────────────────────────────────────────

#: The largest file an upload may be, in bytes. A sheet of two hundred students
#: and a dozen components is under 30 KB, so the limit is generous and still
#: refuses a file that is not a score sheet before it is opened.
IMPORT_MAX_BYTES = 2 * 1024 * 1024

#: The most data rows an upload may hold.
IMPORT_MAX_ROWS = 1000

#: How long a checked upload waits to be confirmed.
IMPORT_VALID_HOURS = 24

#: The file formats an upload may be in, by extension.
IMPORT_FORMATS = ("xlsx", "csv")

#: The sheet of the workbook the template puts the scores on, and the one it
#: explains them on. An upload is read from the first of these it finds, else
#: from the workbook's first sheet.
IMPORT_SCORES_SHEET = "Scores"
IMPORT_NOTES_SHEET = "Notes"

#: The headings of the two fixed columns.
IMPORT_ADMISSION_HEADING = "Admission number"
IMPORT_STUDENT_HEADING = "Student"

#: The longest note a sheet or a class is returned with.
RETURN_NOTE_MAX = 1000

#: Where a submitted sheet goes next.
NEXT_MODERATION = "MODERATION"
NEXT_CLASS_TEACHER = "CLASS_TEACHER"

#: Why a due sheet is not in, in the order the hub looks for a reason.
REASON_RETURNED = "RETURNED"
REASON_NO_MAIN_TEACHER = "NO_MAIN_TEACHER"
REASON_LAST_WORKING_DAY = "LAST_WORKING_DAY"
REASON_CANNOT_SIGN_IN = "CANNOT_SIGN_IN"
REASON_AWAITING_MODERATION = "AWAITING_MODERATION"
REASON_ENTERED = "ENTERED"

#: What a teacher's sheet row invites them to do.
CTA_ENTER = "ENTER"
CTA_CONTINUE = "CONTINUE"
CTA_REVISE = "REVISE"
CTA_VIEW = "VIEW"

#: The kinds of duty on a teacher's hub.
DUTY_CLASS_TEACHER = "CLASS_TEACHER"
DUTY_HEAD_OF_DEPARTMENT = "HEAD_OF_DEPARTMENT"

# ── Compiling and publishing ───────────────────────────────────────────────

#: The longest comment a class teacher or the principal writes for one student.
TEACHER_COMMENT_MAX = 600
PRINCIPAL_COMMENT_MAX = 600

#: The longest reason a hold is placed with, and note it is lifted with.
HOLD_REASON_MAX = 500
HOLD_NOTE_MAX = 500

#: The longest reason an amendment is raised with, and note it is decided with.
AMENDMENT_REASON_MAX = 1000
DECISION_NOTE_MAX = 1000

#: Classes one publish request may name.
PUBLISH_CLASSES_MAX = 50

#: The register fields the attendance block of a compile and the attendance
#: warning of a broadsheet are derived from. A caller who may not read them gets
#: neither, because each states a number the switch withholds.
ATTENDANCE_COUNTS_FIELD = "attendance.register.counts"
ATTENDANCE_STATUS_FIELD = "attendance.register.status"
#: The remark-template switch that also governs the template text printed
#: beside the principal's comment.
REMARK_TEMPLATES_FIELD = "gradebook.setup.remark_templates"

#: The field keys the printed report card and the Results tab are judged by,
#: where a line of text is built from a value a switch governs. The card's
#: ``totals`` lines carry the name of the switch that governs each.
OVERALL_AVERAGE_FIELD = "gradebook.result.overall_average"
POSITION_FIELD = "gradebook.result.position"
RATINGS_FIELD = "gradebook.result.ratings"
ROW_RESULT_FIELD = "gradebook.sheet.row_result"

#: The switch each ``governed_by`` name on a card's ``totals`` line stands for.
CARD_GOVERNED_BY = {
    "row_result": ROW_RESULT_FIELD,
    "overall_average": OVERALL_AVERAGE_FIELD,
    "position": POSITION_FIELD,
}

#: What a card's rating key prints.
RATING_KEY_TEXT = "1 poor \u00b7 2 fair \u00b7 3 good \u00b7 4 very good \u00b7 5 excellent"

# ── Choices ────────────────────────────────────────────────────────────────

class SetupStatus(models.TextChoices):
    """Where an assessment structure or a grading scale stands.

    A DRAFT can be edited and deleted. IN_USE is the one a class is graded by;
    a programme has at most one in use per branch (and one school-wide). RETIRED
    is what an IN_USE one becomes when its replacement is put in use: it stays,
    because published results refer to it.
    """

    DRAFT = "DRAFT", "Draft"
    IN_USE = "IN_USE", "In use"
    RETIRED = "RETIRED", "Retired"


class AmendmentStatus(models.TextChoices):
    """Where a correction to a published result stands.

    PENDING waits for the principal. APPROVED changed the student's result and
    REJECTED changed nothing; both are final and kept.
    """

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class ImportOutcome(models.TextChoices):
    """What confirming an uploaded cell would do to the sheet.

    NEW fills an empty cell, SAME finds the value already there, REPLACES
    overwrites a different value and BLANK is an empty cell in the file, which
    leaves the sheet as it is and never erases.
    """

    NEW = "NEW", "New"
    SAME = "SAME", "Same"
    REPLACES = "REPLACES", "Replaces"
    BLANK = "BLANK", "Blank"


class ImportIssue:
    """The codes an uploaded file or row is refused with in a preview."""

    UNKNOWN_ADMISSION_NUMBER = "UNKNOWN_ADMISSION_NUMBER"
    NOT_ON_THIS_SHEET = "NOT_ON_THIS_SHEET"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    UNREADABLE_VALUE = "UNREADABLE_VALUE"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    MISSING_COLUMN = "MISSING_COLUMN"


class Marker(models.TextChoices):
    """The two things a cell can say instead of a mark.

    ABS counts as zero and keeps its maximum in what the student could obtain.
    EXC removes the component from what the student could obtain.
    """

    ABSENT = "ABS", "Absent"
    EXCUSED = "EXC", "Excused"


class SheetStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not started"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    SUBMITTED = "SUBMITTED", "Submitted"
    APPROVED = "APPROVED", "Approved"
    RETURNED = "RETURNED", "Returned"


class StoredStage(models.TextChoices):
    """The two stages of a class-term that are stored.

    The other two, ENTERING and COMPILING, are worked out from the sheets on
    every read, so they can never disagree with the marks behind them.
    """

    AWAITING = "AWAITING", "Awaiting approval"
    PUBLISHED = "PUBLISHED", "Published"


class ClassStage(models.TextChoices):
    """A class's stage for one term as the screens show it."""

    ENTERING = "ENTERING", "Entering"
    COMPILING = "COMPILING", "Compiling"
    AWAITING = "AWAITING", "Awaiting approval"
    PUBLISHED = "PUBLISHED", "Published"


class CellState(models.TextChoices):
    """What a broadsheet cell or a compile line says about one student in one subject.

    TOTAL carries the subject's total. NOT_TAKEN is an optional subject the
    student does not take. NOT_IN is a subject the student takes whose sheet has
    not come in yet.
    """

    TOTAL = "TOTAL", "Total"
    NOT_TAKEN = "NOT_TAKEN", "Not taken"
    NOT_IN = "NOT_IN", "Not in yet"


class EmailState(models.TextChoices):
    """What became of the report-card-ready email to a student's primary guardian.

    Null is the state before publication. NO_EMAIL is never recorded as sent.
    """

    SENT = "SENT", "Emailed"
    NO_EMAIL = "NO_EMAIL", "No email on file"
    HELD = "HELD", "Held, nothing sent"


class ResultState(models.TextChoices):
    """Where one student's result stands on the release list."""

    NOT_PUBLISHED = "NOT_PUBLISHED", "Not published"
    HELD = "HELD", "Held"
    RELEASED = "RELEASED", "Released"


#: The release list's answer for a released result whose email has no recorded
#: outcome: the send failed or has not finished, and is never read as sent.
EMAIL_NOT_SENT = "NOT_SENT"


class LeftOut(models.TextChoices):
    """Why a student's card is not in a class's print."""

    HELD = "HELD", "Held"
    NOT_RELEASED = "NOT_RELEASED", "Not released"


class HistoryKind(models.TextChoices):
    """The kinds of entry on a student's results history."""

    AMENDMENT = "AMENDMENT", "Amendment"
    HOLD_PLACED = "HOLD_PLACED", "Hold placed"
    HOLD_LIFTED = "HOLD_LIFTED", "Hold lifted"
