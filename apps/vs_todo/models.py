# models.py
# Models for the vs_todo module — "ToDo — Org Accountability".
#
# Contents:
#   TimeStampedModel  - re-used shared abstract base (from vs_user)
#   Task              - one accountable item owned by exactly one CX staff member
#
# The reporting hierarchy that powers roll-up and assignment rules is NOT stored
# here. It is derived live from the CX organogram (Position.reports_to). This
# file holds only the task itself: who owns it, who handed it down, what it
# measures, and whether it is done. Status (Completed / In Progress / Overdue)
# is derived from `is_done` + `deadline`, exactly as in the design.

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from vs_user.models import TimeStampedModel

from .constants import Priority, TaskStatus


class Task(TimeStampedModel):
    """A single accountable item owned by one CX staff member.

    A task is either self-set (``assigned_by`` is NULL) or handed down by a
    manager (``assigned_by`` points at that manager). A manager may only assign
    to someone in their area — enforced in the service/serializer layer against
    the live organogram, since the tree is not stored on the row.
    """

    # ── Ownership ─────────────────────────────────────────────────────────────
    # The person accountable for the task (the design's `staffId`).
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="todo_tasks",
    )
    # The manager who handed this task down. NULL == the assignee set it for
    # themselves. SET_NULL so the task survives the manager's account removal.
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="todo_tasks_assigned",
    )
    # Denormalized snapshot of the inviter's name, so an assigned-by label
    # survives even after the manager's account is removed.
    assigned_by_name = models.CharField(max_length=200, blank=True, default="")

    # ── What the task is ──────────────────────────────────────────────────────
    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    # The success measure and its goal, e.g. metric="Revenue", target="₦120M".
    metric = models.CharField(max_length=120, blank=True, default="")
    target = models.CharField(max_length=120, blank=True, default="")

    deadline = models.DateField()
    priority = models.CharField(
        max_length=8, choices=Priority.choices, default=Priority.MEDIUM,
    )

    # Snapshot of the assignee's department at creation time (derived from their
    # org node). Kept denormalized so historical tasks keep their department even
    # if the person later moves teams — mirrors the design carrying it per task.
    department = models.CharField(max_length=150, blank=True, default="")

    # ── State ─────────────────────────────────────────────────────────────────
    is_done      = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "vs_todo_task"
        ordering = ["is_done", "deadline", "-priority"]
        indexes = [
            models.Index(fields=["assignee", "is_done"]),
            models.Index(fields=["assignee", "deadline"]),
            models.Index(fields=["assigned_by"]),
            models.Index(fields=["is_done", "deadline"]),
        ]

    # ── Validation ────────────────────────────────────────────────────────────
    def clean(self):
        super().clean()
        # A person cannot be recorded as having assigned a task to themselves;
        # that is a self-set task, which is modelled by assigned_by IS NULL.
        if self.assigned_by_id and self.assigned_by_id == self.assignee_id:
            raise ValidationError(
                "A self-set task must leave assigned_by empty, not point at the assignee."
            )

    # ── Derived status (never stored) ─────────────────────────────────────────
    @property
    def status(self) -> str:
        """Completed if done; otherwise Overdue once the deadline has passed,
        else In Progress. Matches the design's taskStatus()."""
        if self.is_done:
            return TaskStatus.COMPLETED
        if self.deadline < timezone.localdate():
            return TaskStatus.OVERDUE
        return TaskStatus.IN_PROGRESS

    @property
    def is_overdue(self) -> bool:
        return self.status == TaskStatus.OVERDUE

    @property
    def is_self_set(self) -> bool:
        return self.assigned_by_id is None

    # ── Transitions ───────────────────────────────────────────────────────────
    def mark_done(self):
        """Idempotently complete the task, stamping the completion time."""
        if not self.is_done:
            self.is_done = True
            self.completed_at = timezone.now()

    def reopen(self):
        """Idempotently move the task back to open."""
        if self.is_done:
            self.is_done = False
            self.completed_at = None

    def __str__(self) -> str:
        return f"Task<{self.pk}:{self.title[:30]}>"
