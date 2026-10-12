"""Names this module's views, services, serializers and seeder must agree on.

Kept in one place so a typo cannot make a view demand a key the seeder never
registers, which fails as a 403 nobody can act on rather than as an error. Same
arrangement as ``vs_calendar.constants``.
"""
from __future__ import annotations

from django.db import models

# ── Permission keys ────────────────────────────────────────────────────────
# Registered, grouped and granted by
# ``core.management.commands.seed_school_permissions``.
#
# A key answers whether somebody may take registers at all. Which class they
# may take is a domain rule enforced in ``services.access``: ``mark`` is the
# class teacher's own class, ``mark_any`` is any class in the branches the
# holder can see.
PERM_REGISTER_VIEW = "attendance.register.view"
PERM_REGISTER_MARK = "attendance.register.mark"
PERM_REGISTER_MARK_ANY = "attendance.register.mark_any"
PERM_REGISTER_REMIND = "attendance.register.remind"
PERM_CORRECTION_CREATE = "attendance.correction.create"
PERM_CORRECTION_APPROVE = "attendance.correction.approve"
PERM_STAFF_VIEW = "attendance.staff.view"
PERM_STAFF_MARK = "attendance.staff.mark"
PERM_REPORT_VIEW = "attendance.report.view"
PERM_REPORT_EXPORT = "attendance.report.export"
PERM_SETTINGS_UPDATE = "attendance.settings.update"

#: The resources whose registered fields Field Access governs.
FIELD_RESOURCE = "attendance.register"
CORRECTION_FIELD_RESOURCE = "attendance.correction"
LESSON_FIELD_RESOURCE = "attendance.lesson"
STAFF_FIELD_RESOURCE = "attendance.staff"

# ── The school's attendance settings ───────────────────────────────────────
# Four school-scoped ``vs_config`` definitions, declared by migration
# ``0002_attendance_settings`` and by ``seed_config_catalogue``, read in
# ``services.rules``.
CFG_GUARDIAN_NOTICES = "attendance.guardian_notices"
CFG_CHRONIC_THRESHOLD = "attendance.chronic_threshold"
CFG_LESSON_MARKING_LEVELS = "attendance.lesson_marking_levels"
CFG_EDIT_WINDOW_DAYS = "attendance.edit_window_days"

DEFAULT_GUARDIAN_NOTICES = True
DEFAULT_CHRONIC_THRESHOLD = 90
CHRONIC_THRESHOLD_MIN = 50
CHRONIC_THRESHOLD_MAX = 100
DEFAULT_EDIT_WINDOW_DAYS = 0
EDIT_WINDOW_MIN = 0
EDIT_WINDOW_MAX = 14

#: The longest a reason's name may be.
REASON_NAME_MAX = 60

#: The reasons every school starts with, in the order a drawer offers them.
#: ``(name, requires_note)``. Only "Other" asks for a note, because it is the
#: one reason that explains nothing by itself.
DEFAULT_REASON_CODES = (
    ("Sick", False),
    ("Medical appointment", False),
    ("Family matter", False),
    ("Religious observance", False),
    ("Travel", False),
    ("Representing the school", False),
    ("Other", True),
)

# ── Notification events ────────────────────────────────────────────────────
EVENT_ABSENCE_NOTICE = "attendance.absence_notice"
EVENT_CORRECTION_NOTICE = "attendance.correction_notice"
EVENT_REGISTER_REMINDER = "attendance.register_reminder"


# ── Choices ────────────────────────────────────────────────────────────────

class Mark(models.TextChoices):
    """One student's attendance on one day.

    Late counts as present and is also counted on its own; the rate is worked
    out in the reports, never stored.
    """

    PRESENT = "P", "Present"
    LATE = "L", "Late"
    ABSENT = "A", "Absent"
    EXCUSED = "E", "Excused"


#: The marks a lesson register takes. A lesson has no Excused: an absence from
#: one lesson is not something a family explains, and no reason code applies.
LESSON_MARKS = (Mark.PRESENT, Mark.LATE, Mark.ABSENT)
LESSON_MARK_CHOICES = [(mark.value, mark.label) for mark in LESSON_MARKS]

#: The marks the staff register takes. Staff are marked on what an
#: administrator saw: present, late (with a time) or absent. There is no
#: Excused, because a member of staff who is away with permission is on leave.
STAFF_MARKS = (Mark.PRESENT, Mark.LATE, Mark.ABSENT)
STAFF_MARK_CHOICES = [(mark.value, mark.label) for mark in STAFF_MARKS]


class StaffLock(models.TextChoices):
    """Why a row on the staff register cannot be marked."""

    ON_LEAVE = "ON_LEAVE", "On approved leave"
    NO_HOME_BRANCH = "NO_HOME_BRANCH", "No home branch"


class NoticeState(models.TextChoices):
    """What became of the absence email to a student's primary guardian.

    NONE is the state before submission, and the state of a mark that sends
    nothing (present, late, excused). The other three are the answers to
    "was the guardian told?": yes, no (no address on file), or no (the school
    has notices off).
    """

    NONE = "NONE", "Not applicable"
    SENT = "SENT", "Sent"
    NO_EMAIL = "NO_EMAIL", "No email on file"
    NOTICES_OFF = "NOTICES_OFF", "Notices are off"


class ChangeVia(models.TextChoices):
    SAME_DAY = "SAME_DAY", "Changed by the person who took the register"
    CORRECTION = "CORRECTION", "Corrected after the day closed"


class CorrectionStatus(models.TextChoices):
    PENDING = "PENDING", "Waiting"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class RegisterStatus(models.TextChoices):
    """Where a class's register for one day stands, worked out on every read."""

    NOT_STARTED = "NOT_STARTED", "Not started"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    READY = "READY", "Ready to submit"
    SUBMITTED = "SUBMITTED", "Submitted"
    CLOSED_DAY = "CLOSED_DAY", "No register today"
