"""Celery tasks for vs_todo.

send_completion_review_request — fired (with a short countdown) when a user
marks their OWN task as done. Emails the reviewer — the manager who assigned
the task, or the assignee's direct line manager for self-set tasks — and drops
an in-app notification so the bell picks it up.

The countdown is the undo window: the task re-checks the Task row at send time
and silently drops the request if the completion was undone (or re-done, which
re-stamps completed_at and queues a fresh request).
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("vs_todo.tasks")


def _fmt_dt(dt) -> str:
    return timezone.localtime(dt).strftime("%d %b %Y, %H:%M") if dt else "—"


def _build_review_email(task, reviewer, assignee) -> tuple[str, str]:
    """Subject + plain-text body for the review request email."""
    subject = f'Review requested: "{task.title}" marked as done'

    lines = [
        f"Hello {reviewer.full_name.split(' ')[0] if reviewer.full_name else 'there'},",
        "",
        f"{assignee.full_name} has marked the task below as completed and it is",
        "awaiting your review.",
        "",
        f"  Task       : {task.title}",
    ]
    if task.description:
        lines.append(f"  Details    : {task.description}")
    lines += [
        f"  Metric     : {task.metric or '—'}",
        f"  Target     : {task.target or '—'}",
        f"  Priority   : {task.get_priority_display()}",
        f"  Deadline   : {task.deadline.strftime('%d %b %Y')}",
        f"  Completed  : {_fmt_dt(task.completed_at)}",
    ]
    if task.department:
        lines.append(f"  Department : {task.department}")
    lines += [
        "",
        "Review it on the console under Tasks → My Team"
        f" → {assignee.full_name.split(' ')[0] if assignee.full_name else 'your team'}.",
        "",
        "— CodeX Vision Console (automated message)",
    ]
    return subject, "\n".join(lines)


@shared_task(bind=True, name="vs_todo.send_completion_review_request")
def send_completion_review_request(self, task_id: int, completed_at: str = ""):
    """Notify the reviewer that a self-completed task awaits their review.

    Queued with countdown=REVIEW_GRACE_SECONDS from services.tasks.set_done.
    Guards make it safe to fire late or more than once:
      * task deleted / reopened within the grace window → skip;
      * completed_at no longer matches the stamp captured at queue time
        (an undo + re-complete queued a fresher request) → skip.
    """
    from vs_notifications.constants import ChannelChoices, NotificationStatus
    from vs_notifications.models import Notification, NotificationEventType
    from vs_notifications.tasks import deliver_email_notification

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

    event, _ = NotificationEventType.objects.get_or_create(
        key=EVENT_TASK_COMPLETED,
        defaults=dict(
            label="Task completed — review requested",
            source_module="vs_todo",
        ),
    )

    subject, body = _build_review_email(task, reviewer, assignee)

    # Email — created PENDING and handed to the standard delivery task so it
    # gets the house retry/idempotency behaviour. Platform-level: school=None.
    email_notif = Notification.objects.create(
        school=None,
        recipient=reviewer,
        event_type=event,
        channel=ChannelChoices.EMAIL,
        subject=subject,
        body=body,
        status=NotificationStatus.PENDING,
    )
    deliver_email_notification.apply_async(args=[str(email_notif.id)])

    # In-app — recorded as SENT directly; the bell poll picks it up.
    Notification.objects.create(
        school=None,
        recipient=reviewer,
        event_type=event,
        channel=ChannelChoices.IN_APP,
        subject=f"Completed task awaiting review: {task.title}",
        body=(
            f'{assignee.full_name} marked "{task.title}" as done. '
            "Kindly review it under Tasks → My Team."
        ),
        status=NotificationStatus.SENT,
    )

    logger.info(
        "Review request for task %s sent to %s (email notif %s).",
        task.pk, reviewer.pk, email_notif.id,
    )
    return {"reviewer": reviewer.pk, "email_notification": str(email_notif.id)}
