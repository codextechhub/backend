"""Constants, enums, and permission keys for vs_todo.

vs_todo is the platform-side "ToDo - Org Accountability" tool used by CodeX
(CX) staff on the internal intranet. It models accountability the same way the
design does: every task belongs to one person, a manager can roll up their whole
area (themselves + everyone beneath them), and assignment only ever flows *down*
the organogram - never sideways or up.

The reporting hierarchy is NOT stored on the task. It is derived live from the
existing CX organogram (vs_user: Position.reports_to / PositionAssignment), so
the ToDo tree always reflects the current org structure. See services/hierarchy.py.
"""
from django.db import models


class Priority(models.TextChoices):
    HIGH   = "HIGH",   "High"
    MEDIUM = "MEDIUM", "Medium"
    LOW    = "LOW",    "Low"


class TaskStatus(models.TextChoices):
    """Derived, never stored - see Task.status. Listed here so the API and the
    frontend share one vocabulary for filtering."""
    COMPLETED   = "COMPLETED",   "Completed"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    OVERDUE     = "OVERDUE",     "Overdue"


# ── RBAC permission keys ──────────────────────────────────────────────────────
# Declared for the RBAC registry. The views gate on CX-staff membership, and
# what a person sees is settled structurally by the organogram: a manager sees
# their own area and assigns down it.
PERM_TASK_VIEW     = "todo.task.view"
PERM_TASK_MANAGE   = "todo.task.manage"
PERM_TASK_ASSIGN   = "todo.task.assign"


# Notification event keys emitted by this app (registered in vs_notifications).
EVENT_TASK_ASSIGNED  = "todo.task_assigned"
EVENT_TASK_COMPLETED = "todo.task_completed"

# Grace window before a self-completion sends its review request. The queued
# task re-reads the state at send time, so undoing inside it cancels the email.
REVIEW_GRACE_SECONDS = 5
