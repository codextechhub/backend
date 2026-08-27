"""The bell schedule: which periods are in force on a given day.

One rule, and it is the one a school has to be told rather than left to infer:
**a weekday holding its own periods replaces the everyday schedule on that day
rather than adding to it.**

A school that defines a full Monday-to-Friday schedule with a null day and then
adds three rows for Friday has a Friday with three periods, not a Friday with
three extra periods. The override is wholesale for exactly one reason: a partial
override would have to say what happens to the ordinary Period 4 when Friday
defines only Periods 1 to 3, and every answer to that is a rule a school would
have to be taught.

The design says the same thing on screen, in these words: "Friday uses its own
schedule (3 periods). The everyday schedule does not apply."
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from ..exceptions import PeriodOverlap, PeriodTimeInvalid
from ..models import Period, PeriodType


def periods_in_force(tenant, session, *, day_of_week, branch=None, queryset=None):
    """The periods that actually run on *day_of_week*, in time order.

    ``branch`` narrows to that branch's own rows plus the school's shared ones,
    which is the inclusive read the whole module uses.

    ``queryset`` may be a queryset **or an already-evaluated list**, and the
    list is the important case: a class grid asks this once per weekday, so
    re-querying would make a five-day grid cost five queries that a single
    prefetch answers. Everything below therefore filters in Python rather than
    in SQL.
    """
    rows = queryset if queryset is not None else Period.objects.filter(
        tenant=tenant, session=session, is_active=True,
    )
    rows = list(rows)

    if branch is not None:
        branch_id = getattr(branch, "id", branch)
        rows = [p for p in rows if p.branch_id is None or p.branch_id == branch_id]

    own = [p for p in rows if p.day_of_week == day_of_week]
    chosen = own if own else [p for p in rows if p.day_of_week is None]
    return sorted(chosen, key=lambda p: (p.start_time, p.order_index))


def day_has_own_schedule(tenant, session, *, day_of_week, branch=None) -> bool:
    """Whether *day_of_week* carries periods of its own.

    What the screen needs to decide between the two halves of its warning: the
    day already replaces the everyday schedule, or adding this row is what will
    make it start replacing it.
    """
    rows = Period.objects.filter(
        tenant=tenant, session=session, day_of_week=day_of_week, is_active=True,
    )
    if branch is not None:
        rows = rows.filter(
            Q(branch__isnull=True)
            | Q(branch_id=branch.id if hasattr(branch, "id") else branch),
        )
    return rows.exists()


def lesson_periods_in_force(tenant, session, *, day_of_week, branch=None):
    """Only the teaching periods. A grid has no row for a break."""
    return [
        p for p in periods_in_force(
            tenant, session, day_of_week=day_of_week, branch=branch,
        )
        if p.period_type == PeriodType.LESSON
    ]


@transaction.atomic
def assert_no_overlap(tenant, session, *, branch, day_of_week, start_time,
                      end_time, exclude_pk=None):
    """Refuse a period that overlaps another on the same day and scope.

    A service rule rather than a database constraint, and the difference is not
    laziness: a range overlap over a nullable branch and a nullable day cannot
    be expressed as a unique index, and PostgreSQL's exclusion constraints are
    used nowhere else in this repository - introducing one here would make this
    module the only place a reviewer meets the mechanism.

    Checked under a row lock in the same transaction as the write, which is the
    shape ``vs_academics``' non-overlap rule on terms already uses.
    """
    if end_time <= start_time:
        raise PeriodTimeInvalid()

    branch_id = getattr(branch, "id", branch)
    rows = (
        Period.all_objects.select_for_update()
        .filter(
            tenant=tenant, session=session, day_of_week=day_of_week,
            branch_id=branch_id, is_active=True,
        )
        .order_by("pk")
    )
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)

    for other in rows:
        if other.start_time < end_time and other.end_time > start_time:
            raise PeriodOverlap(
                f"This overlaps {other.label} "
                f"({_t(other.start_time)} - {_t(other.end_time)}) on the same "
                f"day and scope.",
                period_id=other.pk,
                label=other.label,
            )


def provisional_order_index(tenant, session, *, branch, day_of_week,
                            exclude_pk=None) -> int:
    """A free slot at the end of the day, to be corrected by ``renumber_day``.

    Not the row's final position, and deliberately not. Computing the real
    position up front and inserting there collides with the row already holding
    it: adding an 07:30 assembly to a day that starts at 08:00 wants index 1,
    and Period 1 has index 1 until something moves it. The unique constraint
    refuses the insert and the caller sees DUPLICATE for a period that is not a
    duplicate of anything.

    So a period is parked past the end of the day and ``renumber_day`` puts the
    whole day in time order immediately afterwards, in the same transaction.
    """
    branch_id = getattr(branch, "id", branch)
    rows = Period.all_objects.filter(
        tenant=tenant, session=session, day_of_week=day_of_week,
        branch_id=branch_id,
    )
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)
    highest = max((p.order_index for p in rows), default=0)
    return highest + 1


def renumber_day(tenant, session, *, branch, day_of_week):
    """Put a day's periods back in time order after an insert or an edit.

    Two passes, because ``order_index`` is under a unique constraint per scope
    and a single pass would collide with a row it has not moved yet.
    """
    branch_id = getattr(branch, "id", branch)
    rows = list(
        Period.all_objects.filter(
            tenant=tenant, session=session, day_of_week=day_of_week,
            branch_id=branch_id,
        ).order_by("start_time", "pk"),
    )
    if not rows:
        return
    offset = 1000
    for index, row in enumerate(rows, start=1):
        Period.all_objects.filter(pk=row.pk).update(order_index=offset + index)
    for index, row in enumerate(rows, start=1):
        Period.all_objects.filter(pk=row.pk).update(order_index=index)


def _t(value) -> str:
    """A time the way the product writes it: 8:00 am, not 08:00:00."""
    hour = value.hour % 12 or 12
    suffix = "am" if value.hour < 12 else "pm"
    return f"{hour}:{value.minute:02d} {suffix}"
