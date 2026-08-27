"""Names this module's views, services, serializers and seeder must agree on.

Kept in one place so a typo cannot make a view demand a key the seeder never
registers, which fails as a 403 nobody can act on rather than as an error. Same
arrangement as ``vs_academics.constants``.
"""
from __future__ import annotations

# ── Permission keys ────────────────────────────────────────────────────────
# The calendar four are already seeded by
# ``core.management.commands.seed_school_permissions`` and were registered,
# grouped and granted long before anything used them. The timetable five are
# added by the same command in this change (FRD v3.0.1 section 7.1).
PERM_CALENDAR_VIEW = "academics.calendar.view"
PERM_CALENDAR_CREATE = "academics.calendar.create"
PERM_CALENDAR_UPDATE = "academics.calendar.update"
PERM_CALENDAR_MANAGE = "academics.calendar.manage"

PERM_TIMETABLE_VIEW = "academics.timetable.view"
PERM_TIMETABLE_CREATE = "academics.timetable.create"
PERM_TIMETABLE_UPDATE = "academics.timetable.update"
PERM_TIMETABLE_MANAGE = "academics.timetable.manage"
PERM_TIMETABLE_PUBLISH = "academics.timetable.publish"


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
