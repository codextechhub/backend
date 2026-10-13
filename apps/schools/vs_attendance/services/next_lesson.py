"""A teacher's next lesson, from now.

**What counts.** A lesson is a slot the timetable gives the teacher, in a
teaching period still in use, of an active class of the active school year
whose timetable is published. A draft timetable has no lessons yet: announcing
one would send a teacher to a room the school has not agreed. It does not
matter whether the class's level marks lessons: this is the timetable's
answer, not the register's.

**When.** The first such lesson that has not started yet: later today at the
class's own branch clock, or on the first day after today that the school is
open to the class (:class:`.days.DayCalendar`, so a holiday, a closure for the
branch or the level, and a day outside term are skipped). The search looks a
fortnight ahead, which covers every half-term break, and answers ``None`` past
that rather than walking the calendar indefinitely.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_academics.models import SessionStatus
from schools.vs_calendar.models import ClassTimetable, PeriodType, PublishState, TimetableSlot
from vs_config.clock import branch_now
from vs_config.display import format_date
from vs_rbac.scoping import WHOLE_TENANT

from .days import DayCalendar
from .scope import placed_classes, school_branches

#: How many days ahead the search looks before it gives up.
HORIZON_DAYS = 14


def next_lesson(tenant, user) -> dict | None:
    """The next lesson the timetable gives *user*, or ``None`` within the horizon."""
    slots = list(
        TimetableSlot.objects.filter(
            tenant=tenant, teacher=user,
            period__period_type=PeriodType.LESSON, period__is_active=True,
            school_class__is_active=True, school_class__session__status=SessionStatus.ACTIVE,
            school_class__in=ClassTimetable.objects.filter(
                tenant=tenant, status=PublishState.PUBLISHED,
            ).values("school_class"),
        ).select_related(
            "period", "subject", "room", "school_class", "school_class__session",
            "school_class__level", "school_class__branch",
        ),
    )
    if not slots:
        return None

    branches = school_branches(tenant)
    classes = {slot.school_class_id: slot.school_class for slot in slots}
    # The teacher's own lessons, wherever they are: their reach is not the question.
    placed, _ = placed_classes(list(classes.values()), branches, WHOLE_TENANT)
    homes = {school_class.pk: home for school_class, home in placed}
    now_at = {home.pk: branch_now(tenant, home) for home in homes.values()}
    if not now_at:
        return None

    start = min(now.date() for now in now_at.values())
    end = start + dt.timedelta(days=HORIZON_DAYS)
    calendar = DayCalendar(
        tenant, {c.session for c in classes.values()}, start, end,
    )

    best = None
    for slot in slots:
        home = homes.get(slot.school_class_id)
        if home is None:
            continue
        now = now_at[home.pk]
        for offset in range(HORIZON_DAYS + 1):
            day = now.date() + dt.timedelta(days=offset)
            if day.isoweekday() != slot.day_of_week:
                continue
            if offset == 0 and slot.period.start_time <= now.time():
                continue
            if not calendar.status(slot.school_class, home, day).is_open:
                continue
            key = (day, slot.period.start_time, slot.school_class.name.lower(), slot.pk)
            if best is None or key < best[0]:
                best = (key, slot, day, home, now.date())
            break

    if best is None:
        return None
    _, slot, day, home, today = best
    return {
        "date": day,
        "date_label": format_date(day, tenant),
        "is_today": day == today,
        "period_label": slot.period.label,
        "start_time": slot.period.start_time,
        "end_time": slot.period.end_time,
        "subject": slot.subject.name,
        "class_name": slot.school_class.name,
        "room_name": slot.room.name if slot.room_id else None,
        "branch_name": home.name if len(branches) > 1 else None,
    }
