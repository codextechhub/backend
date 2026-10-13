"""The administrator's list of a day's lessons.

``GET /v1/attendance/lessons/`` lists every lesson of the day in the classes the
caller can see, so an administrator can see which lessons have been marked and
which are waiting. Each row is one lesson: its period, class, subject and
teacher, and where its register stands (:mod:`.lessons`).

**Scope.** The same as the hub's (:mod:`.hub`): a branch-bound caller sees their
own branches' classes and a whole-school caller sees every class, ``branch``
narrows either to one, and a class with no branch at a school that has several
has no lessons to list. A class with nobody placed in it has no register to
take and is not listed. A day not yet come at a class's branch, and a day the
school is closed to the class, list nothing for it.

**When lesson marking is off everywhere** the list is empty and says so
(``lesson_marking_on`` is false), so a screen can tell "no lessons today" from
"this school does not mark lessons".
"""
from __future__ import annotations

from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_rbac.scoping import visible_branch_ids

from . import lessons
from schools.vs_students.services.roster import rosters_as_at
from .scope import (
    active_classes,
    placed_classes,
    resolve_day,
    school_branches,
    whole_day_closed,
)


def lessons_list(tenant, user, *, on_date=None, branch=None) -> dict:
    """The day's lessons for *user*'s scope, before Field Access is applied."""
    branches = school_branches(tenant)
    several = len(branches) > 1
    visible = visible_branch_ids(user, tenant)
    levels = lessons.marking_levels(tenant)

    placed, _ = placed_classes(active_classes(tenant), branches, visible, branch)
    default_today = branch_today(tenant, branch) if branch is not None else None
    on_date, today_by_branch, calendar = resolve_day(tenant, placed, on_date, default_today)
    days = {c.pk: calendar.status(c, home, on_date) for c, home in placed}
    body = {
        "date": on_date,
        "date_label": format_date(on_date, tenant),
        "closed": whole_day_closed(placed, days),
        "lesson_marking_on": bool(levels),
        "branch_dimension": several,
        "lessons": [],
    }
    if not levels or body["closed"] is not None:
        return body

    due = [(c, home) for c, home in placed if on_date <= today_by_branch[home.pk]]
    found = lessons.day_lessons(tenant, due, days, on_date, levels)
    classes = {lesson.school_class.pk: lesson.school_class for lesson in found}
    rosters = rosters_as_at(tenant, list(classes.values()), on_date)
    found = [lesson for lesson in found if rosters.get(lesson.school_class.pk)]
    lessons.with_state(tenant, found, on_date, days, rosters)

    body["lessons"] = [
        {
            "slot_id": lesson.slot.pk,
            "period_label": lesson.period_label,
            "start_time": lesson.starts_at,
            "end_time": lesson.ends_at,
            "school_class": {"id": lesson.school_class.pk, "name": lesson.school_class.name},
            "branch": {"id": lesson.home.pk, "name": lesson.home.name} if several else None,
            "subject": lesson.subject_name,
            "teacher_name": lesson.teacher_name,
            "status": lesson.status,
            "submitted_at": lesson.submitted_at,
            "submitted_by_name": lesson.submitted_by_name,
        }
        for lesson in found
    ]
    return body
