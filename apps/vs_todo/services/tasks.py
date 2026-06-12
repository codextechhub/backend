"""Task lifecycle operations for vs_todo.

Centralises the rules the design encodes in App.submitTask / toggle:
  * a self-set task has no assigned_by;
  * an assigned task must target someone in the manager's area (assign down only);
  * the department is snapshotted from the assignee at creation;
  * completing/reopening flips is_done and stamps completed_at.

Keeping these here (rather than in the serializer) means the same guarantees hold
whether a task is created through the API, the admin, or a management command.
"""
from __future__ import annotations

from typing import Optional

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from vs_user.models import User

from .hierarchy import TodoHierarchy
from ..constants import Priority
from ..models import Task


def _department_for(user: User) -> str:
    """Best-effort department-name snapshot for a CX staff member."""
    profile = getattr(user, "platform_staff_profile", None)
    if profile is None:
        return ""
    dept = profile.department  # OrgNode of kind DEPARTMENT, or None
    return dept.name if dept is not None else ""


@transaction.atomic
def create_task(
    *,
    actor: User,
    title: str,
    deadline,
    assignee: Optional[User] = None,
    description: str = "",
    metric: str = "",
    target: str = "",
    priority: str = Priority.MEDIUM,
) -> Task:
    """Create a task.

    If ``assignee`` is omitted or is ``actor``, the task is self-set. Otherwise
    it is an assignment and ``actor`` must manage ``assignee`` — assignment only
    ever flows down the organogram.
    """
    is_assignment = assignee is not None and assignee.pk != actor.pk
    if is_assignment:
        if not TodoHierarchy.can_assign(actor, assignee):
            raise PermissionDenied(
                "You can only assign tasks to people within your team."
            )
    else:
        assignee = actor

    task = Task(
        assignee=assignee,
        assigned_by=actor if is_assignment else None,
        assigned_by_name=actor.full_name if is_assignment else "",
        title=title,
        description=description,
        metric=metric,
        target=target,
        deadline=deadline,
        priority=priority,
        department=_department_for(assignee),
    )
    task.full_clean()
    task.save()
    return task


@transaction.atomic
def set_done(task: Task, *, done: bool, actor: Optional[User] = None) -> Task:
    """Complete or reopen a task and persist the change.

    When the ASSIGNEE completes their own task (actor == assignee), a review
    request to their reviewer (the assigner, else their direct line manager) is
    queued with a short countdown — the undo window. The Celery task re-checks
    the row at send time, so reopening within the window cancels the email.
    A manager toggling a report's task never triggers the flow.
    """
    was_done = task.is_done
    if done:
        task.mark_done()
    else:
        task.reopen()
    task.save(update_fields=["is_done", "completed_at", "updated_at"])

    is_self_completion = (
        done and not was_done and actor is not None and actor.pk == task.assignee_id
    )
    if is_self_completion:
        from ..constants import REVIEW_GRACE_SECONDS
        from ..tasks import send_completion_review_request

        stamp = task.completed_at.isoformat() if task.completed_at else ""
        task_pk, actor_pk, title = task.pk, actor.pk, task.title
        transaction.on_commit(
            lambda: send_completion_review_request.apply_async(
                kwargs={
                    "task_id": task_pk,
                    "completed_at": stamp,
                    # Queue-row attribution (View Queues page).
                    "_job_owner_id": str(actor_pk),
                    "_job_label": f"Review request: {title}"[:255],
                    "_job_kind": "email",
                },
                countdown=REVIEW_GRACE_SECONDS,
            )
        )
    return task


def can_view_task(viewer: User, task: Task) -> bool:
    """A viewer may see a task if it falls within their area (own + reports)."""
    return task.assignee_id in TodoHierarchy.area_user_ids(viewer)


def can_modify_task(viewer: User, task: Task) -> bool:
    """The assignee or anyone above them in the chain may toggle/edit a task."""
    if task.assignee_id == viewer.pk:
        return True
    return TodoHierarchy.can_assign(viewer, task.assignee)
