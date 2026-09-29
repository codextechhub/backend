"""Names this module's views, services, serializers and seeder must agree on.

Kept in one place so a typo cannot make a view demand a key the seeder never
registers, which fails as a 403 nobody can act on rather than as an error. Same
arrangement as ``vs_academics.constants``.
"""
from __future__ import annotations

from django.db import models

# ── Permission keys ────────────────────────────────────────────────────────
# The calendar four are already seeded by
# ``core.management.commands.seed_school_permissions`` and were registered,
# grouped and granted long before anything used them. The timetable five are
# added by the same command in this change (FRD v3.0.1 section 7.1).
PERM_CALENDAR_VIEW = "academics.calendar.view"
PERM_CALENDAR_CREATE = "academics.calendar.create"
PERM_CALENDAR_UPDATE = "academics.calendar.update"
PERM_CALENDAR_DELETE = "academics.calendar.delete"

PERM_TIMETABLE_VIEW = "academics.timetable.view"
PERM_TIMETABLE_CREATE = "academics.timetable.create"
PERM_TIMETABLE_UPDATE = "academics.timetable.update"
PERM_TIMETABLE_DELETE = "academics.timetable.delete"
PERM_TIMETABLE_PUBLISH = "academics.timetable.publish"

#: Exams have their own keys rather than borrowing the timetable's. Sharing
#: them meant exam scheduling and the weekly class timetable could not be sold
#: at different depths: one band had to cover both.
PERM_EXAM_VIEW = "academics.exam.view"
PERM_EXAM_CREATE = "academics.exam.create"
PERM_EXAM_UPDATE = "academics.exam.update"
PERM_EXAM_DELETE = "academics.exam.delete"
PERM_EXAM_PUBLISH = "academics.exam.publish"


#: The key that changes a school's own settings, held apart from this module's
#: keys: the calendar and timetable settings bind every screen here, and are
#: the school's settings rather than a piece of its timetable.
PERM_SETTINGS_UPDATE = "school.settings.update"


# ── The school's calendar and timetable settings ───────────────────────────
# Seven school-scoped ``vs_config`` definitions, declared by migration
# ``0002_calendar_and_timetable_settings`` and by ``seed_config_catalogue``,
# read and written in ``services.calendar_rules``.
CFG_TEACHING_DAYS = "calendar.teaching_days"
CFG_WEEK_STARTS_ON = "calendar.week_starts_on"
CFG_CLOSES_SCHOOL_BY_TYPE = "calendar.closes_school_by_type"
CFG_ROOM_REQUIRED_TO_PUBLISH = "timetable.room_required_to_publish"
CFG_TEACHER_DUTY_MATCH = "timetable.teacher_duty_match"
CFG_INVIGILATOR_ROLES = "exams.invigilator_roles"
CFG_DEFAULT_PERIOD_MINUTES = "timetable.default_period_minutes"

#: ISO weekdays, Monday to Friday.
DEFAULT_TEACHING_DAYS = (1, 2, 3, 4, 5)
#: The two days a school's week may start on: Monday (ISO 1) or Sunday (ISO 7).
WEEK_STARTS = (1, 7)
DEFAULT_INVIGILATOR_ROLES = ("teacher",)
PERIOD_MINUTES_MIN = 10
PERIOD_MINUTES_MAX = 240


class DutyMatch(models.TextChoices):
    """How the timetable treats a teacher with no teaching duty for the lesson.

    A teaching duty is ``vs_staff``'s record that a person teaches a subject
    to a class in a session. OFF ignores it, WARN saves and says so, REFUSE
    refuses the save and blocks publishing while any such lesson remains.
    """

    OFF = "OFF", "Off: anyone with the teacher role may take any lesson"
    WARN = "WARN", "Warn when the teacher has no teaching duty for the class and subject"
    REFUSE = "REFUSE", "Refuse a teacher with no teaching duty for the class and subject"


# ── Warning codes ──────────────────────────────────────────────────────────
# A warning is not a refusal. It travels in ``data.warnings`` as a list of
# ``{"code": ..., "detail": ...}`` beside the row that was written, because the
# write succeeded and the school needs to see what it just did. FRD FR-014.
WARN_TEACHER_DOUBLE_BOOKED = "TEACHER_DOUBLE_BOOKED"
WARN_ROOM_DOUBLE_BOOKED = "ROOM_DOUBLE_BOOKED"
WARN_CLASS_DOUBLE_BOOKED = "CLASS_DOUBLE_BOOKED"
WARN_INVIGILATOR_DOUBLE_BOOKED = "INVIGILATOR_DOUBLE_BOOKED"
WARN_EVENT_OUTSIDE_ANY_TERM = "EVENT_OUTSIDE_ANY_TERM"
WARN_EVENT_OVERLAP = "EVENT_OVERLAP"
#: A lesson whose teacher holds no teaching duty for its class and subject in
#: its session, under ``DutyMatch.WARN`` (and listed, not refused, by the grid
#: read under REFUSE, because a duty withdrawn after the save leaves one).
WARN_TEACHER_HAS_NO_DUTY = "TEACHER_HAS_NO_DUTY"

# ── Alert codes, for the overview (FR-007) ─────────────────────────────────
ALERT_SESSION_HAS_NO_TERMS = "SESSION_HAS_NO_TERMS"
ALERT_EVENT_OUTSIDE_ANY_TERM = "EVENT_OUTSIDE_ANY_TERM"
ALERT_TERM_OUTSIDE_SESSION = "TERM_OUTSIDE_SESSION"
ALERT_TERM_DATES_OVERLAP = "TERM_DATES_OVERLAP"
#: Added by the design reconciliation. FR-007 as written forbids these two,
#: which is v2.3 text carried forward from when the timetable half was
#: deferred; the hub screen shows both. See docs/timetable-api-plan.md 4.1.
ALERT_TIMETABLE_HAS_CLASHES = "TIMETABLE_HAS_CLASHES"
ALERT_CLASS_HAS_NO_TIMETABLE = "CLASS_HAS_NO_TIMETABLE"
