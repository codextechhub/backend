"""Roll-up dashboard assembly for vs_todo.

Two shapes, matching the design's screens:

  * ``node_dashboard`` — one person's view (My Tasks / a drilled-into report):
    their own task list and headline, their *area* headline (self + everyone
    beneath), a card per direct report, and the breadcrumb up to the root.

  * ``org_rollup`` — the organogram tree rooted at a person, every node carrying
    its own + area completion stats. Built from a single task fetch and one pass
    over the position tree so it stays cheap regardless of depth.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from vs_user.models import Position, User

from ..models import Task
from .hierarchy import TodoHierarchy
from .stats import area_tasks_qs, own_tasks_qs, stats_for


def node_dashboard(focus: User) -> dict:
    """The dashboard for a single person (the design's NodeDashboard)."""
    own = list(own_tasks_qs(focus).select_related("assignee", "assigned_by"))
    reports = []
    for report in TodoHierarchy.direct_report_users(focus):
        reports.append({
            "person": report,
            "is_manager": TodoHierarchy.is_manager(report),
            "area_stats": stats_for(area_tasks_qs(report)),
        })
    return {
        "person": focus,
        "is_manager": bool(reports),
        "own_tasks": own,
        "own_stats": stats_for(own),
        "area_stats": stats_for(area_tasks_qs(focus)),
        "reports": reports,
        "breadcrumb": TodoHierarchy.chain_to(focus),
    }


def org_rollup(root: User) -> Optional[dict]:
    """The organogram subtree rooted at ``root`` with per-node roll-up stats."""
    root_pos = TodoHierarchy.primary_position(root)
    if root_pos is None:
        # No seat in the org — fall back to a single self node.
        own = list(own_tasks_qs(root))
        return {
            "person": root, "is_manager": False,
            "own_stats": stats_for(own), "area_stats": stats_for(own),
            "direct_reports": [],
        }

    index = TodoHierarchy._children_index()

    # One task fetch for the whole area, grouped by assignee.
    area_ids = TodoHierarchy.area_user_ids(root)
    tasks_by_user: Dict[int, List[Task]] = defaultdict(list)
    for task in Task.objects.filter(assignee_id__in=area_ids):
        tasks_by_user[task.assignee_id].append(task)

    def build(position: Position) -> Optional[dict]:
        holder = position.current_holder
        own = tasks_by_user.get(holder.pk, []) if holder else []
        area = list(own)
        children = []
        for child_pos in index.get(position.pk, []):
            node = build(child_pos)
            if node is None:
                continue
            children.append(node)
            area.extend(node.pop("_area_tasks"))
        # Drop empty branches: a vacant seat with no reports carries nothing.
        if holder is None and not children:
            return None
        return {
            "person": holder,
            "is_manager": bool(children),
            "own_stats": stats_for(own),
            "area_stats": stats_for(area),
            "direct_reports": children,
            "_area_tasks": area,
        }

    tree = build(root_pos)
    if tree is not None:
        tree.pop("_area_tasks", None)
    return tree
