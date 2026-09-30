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

from vs_config.display import format_time

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
    shape ``vs_academics``' non-overlap rule on terms already uses. The refusal
    names the period it collides with and that period's times on the school's
    own clock ("8:00 am" or "08:00").
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
                f"({format_time(other.start_time, tenant)} - "
                f"{format_time(other.end_time, tenant)}) on the same "
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


@transaction.atomic
def copy_bell_schedule(tenant, *, source, target, visible, teaching_days):
    """Copy *source*'s periods into *target*, which must have none of its own.

    Answers ``(created, skipped)``: the new periods, and the source periods
    left out.

    Every period is copied as it stands: its branch, its day, its position,
    its times, its type and whether it is active. So a school whose Ikeja
    branch rings its own bell and whose Friday runs a short day gets both
    again in the new year. Nothing is copied into a year that already has
    periods, because merging two schedules would have to decide which of two
    Period 1s wins.

    A period set for a day that is not one of *teaching_days* (the school's
    ``calendar.teaching_days``) is left out, because adding it by hand would be
    refused: a school that stopped teaching Saturdays does not get last year's
    Saturday periods back. An every-day period is always copied. When every
    period the caller may copy would be left out, the copy is refused with the
    reason rather than answering with nothing.

    *visible* is the caller's branch reach (``WHOLE_TENANT`` or a set of
    branch ids). A branch-bound caller copies only the periods at their own
    branches, and is refused only when the target already has periods there:
    the school's shared periods are a school-wide administrator's to copy, the
    same rule that keeps a branch-bound caller from creating one.

    The target year's row is locked for the length of the copy, so two copies
    sent at once cannot both find it empty.
    """
    from rest_framework.exceptions import PermissionDenied

    from schools.vs_academics.models import AcademicSession
    from vs_rbac.scoping import WHOLE_TENANT

    from ..exceptions import BellScheduleNotEmpty, NothingToCopy

    AcademicSession.all_objects.select_for_update().filter(pk=target.pk).first()

    rows = Period.all_objects.filter(tenant=tenant, session=source)
    existing = Period.all_objects.filter(tenant=tenant, session=target)
    where = ""
    if visible is not WHOLE_TENANT:
        if not visible:
            raise PermissionDenied(
                "Your access to every branch has been withdrawn, so you cannot "
                "create anything here. Ask a school administrator to restore it.",
            )
        rows = rows.filter(branch_id__in=visible)
        existing = existing.filter(branch_id__in=visible)
        where = " at your branch" if len(visible) == 1 else " at your branches"

    rows = list(rows.order_by("branch_id", "day_of_week", "order_index", "pk"))
    skipped = [
        row for row in rows
        if row.day_of_week is not None and row.day_of_week not in teaching_days
    ]
    if rows and len(skipped) == len(rows):
        which = (
            "is not a teaching day" if len(_days(skipped)) == 1
            else "are not teaching days"
        )
        raise NothingToCopy(
            f"Every period{where} in {source.name} is set for "
            f"{days_phrase(skipped)}, which {which}, so there is nothing to "
            f"copy. Add {days_phrase(skipped)} to the teaching days in "
            f"Settings, Calendar and timetables first, or build "
            f"{target.name}'s bell schedule by hand.",
            field="from_session",
        )
    rows = [row for row in rows if row not in skipped]
    if not rows:
        raise NothingToCopy(
            f"{source.name} has no periods{where} to copy."
            + (
                " The school's shared periods are copied by a school-wide "
                "administrator."
                if where else ""
            ),
            field="from_session",
        )
    if existing.exists():
        raise BellScheduleNotEmpty(
            f"{target.name} already has periods{where}, so nothing was copied. "
            f"Copying fills an empty bell schedule: change {target.name}'s "
            f"periods on the Bell schedule instead.",
        )

    created = Period.all_objects.bulk_create([
        Period(
            tenant=tenant, session=target, branch_id=row.branch_id,
            day_of_week=row.day_of_week, order_index=row.order_index,
            label=row.label, period_type=row.period_type,
            start_time=row.start_time, end_time=row.end_time,
            is_active=row.is_active,
        )
        for row in rows
    ])
    return list(
        Period.all_objects.filter(pk__in=[row.pk for row in created])
        .select_related("branch")
        .order_by("day_of_week", "start_time", "pk"),
    ), skipped


def _days(periods) -> list:
    return sorted({row.day_of_week for row in periods})


def days_phrase(periods) -> str:
    """The weekdays *periods* are set for, as prose: "Saturday and Sunday"."""
    from ..models import DayOfWeek

    names = [DayOfWeek(day).label for day in _days(periods)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def skipped_sentence(skipped) -> str:
    """Why periods were left out of a copy, or "" when none were.

    "2 Saturday periods were left out because Saturday is not a teaching day."
    """
    if not skipped:
        return ""
    count, days = len(skipped), _days(skipped)
    noun = "period was" if count == 1 else "periods were"
    if len(days) == 1:
        day = days_phrase(skipped)
        return f"{count} {day} {noun} left out because {day} is not a teaching day."
    return (
        f"{count} {noun} left out because {days_phrase(skipped)} are not "
        f"teaching days."
    )
