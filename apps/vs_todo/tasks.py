"""Celery tasks for vs_todo.

send_completion_review_request — fired (with a short countdown) when a user
marks their OWN task as done. Notifies the reviewer — the manager who assigned
the task, or the assignee's direct line manager for self-set tasks — via the
vs_notifications engine on both the email and in-app channels (the bell picks
up the in-app record).

The countdown is the undo window: the task re-checks the Task row at send time
and silently drops the request if the completion was undone (or re-done, which
re-stamps completed_at and queues a fresh request).
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("vs_todo.tasks")


def _first_name(user) -> str:
    return user.full_name.split(" ")[0] if user.full_name else ""


def _fmt_dt(dt) -> str:
    return timezone.localtime(dt).strftime("%d %b %Y, %H:%M") if dt else "—"


@shared_task(bind=True, name="vs_todo.send_completion_review_request")
def send_completion_review_request(self, task_id: int, completed_at: str = ""):
    """Notify the reviewer that a self-completed task awaits their review.

    Queued with countdown=REVIEW_GRACE_SECONDS from services.tasks.set_done.
    Guards make it safe to fire late or more than once:
      * task deleted / reopened within the grace window → skip;
      * completed_at no longer matches the stamp captured at queue time
        (an undo + re-complete queued a fresher request) → skip.

    Dispatch goes through the vs_notifications engine (todo.task_completed,
    seeded via the event registry). An unseeded registry would raise
    UnknownEventTypeError — swallowed and reported as a skip so a missing seed
    never crashes the task.
    """
    from vs_notifications.exceptions import UnknownEventTypeError
    from vs_notifications.notify import send_notification

    from .constants import EVENT_TASK_COMPLETED
    from .models import Task
    from .services.hierarchy import TodoHierarchy

    try:
        task = Task.objects.select_related("assignee", "assigned_by").get(pk=task_id)
    except Task.DoesNotExist:
        return {"skipped": "task-deleted"}

    if not task.is_done or task.completed_at is None:
        return {"skipped": "reopened-within-grace"}
    if completed_at and task.completed_at.isoformat() != completed_at:
        return {"skipped": "superseded-by-newer-completion"}

    assignee = task.assignee
    reviewer = task.assigned_by or TodoHierarchy.direct_manager(assignee)
    if reviewer is None or reviewer.pk == assignee.pk:
        return {"skipped": "no-reviewer"}

    context = {
        "reviewer_name":    reviewer.full_name,
        "reviewer_first":   _first_name(reviewer) or "there",
        "assignee_name":    assignee.full_name,
        "assignee_first":   _first_name(assignee) or "your team",
        "task_title":       task.title,
        "task_description": task.description or "",
        "task_metric":      task.metric or "—",
        "task_target":      task.target or "—",
        "task_priority":    task.get_priority_display(),
        "task_deadline":    task.deadline.strftime("%d %b %Y"),
        "task_completed":   _fmt_dt(task.completed_at),
        "task_department":  task.department or "",
    }

    # Platform-level tool (CX staff, no school). The engine creates the email
    # (PENDING → delivery task) and in-app (SENT) records for todo.task_completed.
    try:
        created_ids = send_notification(
            event_key=EVENT_TASK_COMPLETED,
            context=context,
            recipients=[reviewer],
            school=None,
        )
    except UnknownEventTypeError:
        logger.error(
            "send_completion_review_request: event %s is not seeded — skipping task %s.",
            EVENT_TASK_COMPLETED, task.pk,
        )
        return {"skipped": "event-not-seeded"}

    logger.info(
        "Review request for task %s dispatched to %s (%d record(s)).",
        task.pk, reviewer.pk, len(created_ids),
    )
    return {"reviewer": reviewer.pk, "notifications": created_ids}
