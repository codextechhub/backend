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
def set_done(task: Task, *, done: bool) -> Task:
    """Complete or reopen a task and persist the change."""
    if done:
        task.mark_done()
    else:
        task.reopen()
    task.save(update_fields=["is_done", "completed_at", "updated_at"])
    return task


def can_view_task(viewer: User, task: Task) -> bool:
    """A viewer may see a task if it falls within their area (own + reports)."""
    return task.assignee_id in TodoHierarchy.area_user_ids(viewer)


def can_modify_task(viewer: User, task: Task) -> bool:
    """The assignee or anyone above them in the chain may toggle/edit a task."""
    if task.assignee_id == viewer.pk:
        return True
    return TodoHierarchy.can_assign(viewer, task.assignee)
