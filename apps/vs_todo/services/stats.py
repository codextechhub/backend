"""Roll-up statistics for vs_todo.

Mirrors the design's statsFor() / ownTasks() / areaTasks(): turn a set of tasks
into the completion headline (done / in-progress / overdue / pct) that the rings
and pills render from.
"""
from __future__ import annotations

from typing import Iterable

from ..constants import TaskStatus
from ..models import Task
from .hierarchy import TodoHierarchy


def stats_for(tasks: Iterable[Task]) -> dict:
    """Counts + completion percentage for a collection of tasks."""
    total = done = in_progress = overdue = 0
    for task in tasks:
        total += 1
        status = task.status
        if status == TaskStatus.COMPLETED:
            done += 1
        elif status == TaskStatus.OVERDUE:
            overdue += 1
        else:
            in_progress += 1
    pct = round((done / total) * 100) if total else 0
    return {
        "total": total,
        "done": done,
        "in_progress": in_progress,
        "overdue": overdue,
        "pct": pct,
    }


def own_tasks_qs(user):
    """Just the tasks the person is personally accountable for."""
    return Task.objects.filter(assignee=user)


def area_tasks_qs(user):
    """The person's tasks plus everyone's beneath them (design: areaTasks)."""
    return Task.objects.filter(assignee_id__in=TodoHierarchy.area_user_ids(user))
