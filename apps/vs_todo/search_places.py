"""The to-do board the search bar can route to.

The board is a CX staff tool that asks no key
(:data:`vs_todo.views.TODO_PERMISSIONS`), so its place needs none and names
the ``platform_staff`` condition, which asks the same
:class:`vs_rbac.permissions.IsVisionStaff`.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

register(
    Place(
        kind=ACTION, key="todo.board", label="To-do board",
        description="Your tasks and the tasks of the people who report to you.",
        keywords=("tasks", "todo", "assignments"),
        screen="todo.board", condition="platform_staff",
    ),
)
