"""Writing a class's week: what a slot must satisfy, and copying a whole grid.

The branch rules here are the ones a multi-branch school's defects come from, so
they are written once and called from every write path.

A slot has three parents that each carry a branch or could - a class, a period
and a room - and they can disagree. Which of them decides:

* **The room decides where the lesson is.** A slot carries no branch column,
  deliberately: it would put the same value in every row of a grid, be a second
  answer to a question the room already answers, and be free to contradict it.
* **A class bound to a branch may only use that branch's rooms**
  (``ROOM_BRANCH_CONFLICT``). This is the containment invariant M13 applies to
  its own hierarchy, applied to a physical place - and it is a different code
  from ``BRANCH_SCOPE_CONFLICT`` on purpose, because a school reading the
  message needs to know which of the two it has hit.
* **A period must be the school's or the room's branch's**
  (``BRANCH_SCOPE_CONFLICT``). A branch that rings its own bell may not have
  another branch's lesson scheduled against it.
* **One class's week may not span two branches**
  (``TIMETABLE_SPANS_BRANCHES``). A school-wide class is visible to both
  branches' admins under the inclusive read, so without this rule two of them
  can each start building JSS1 A's grid in their own rooms and the system
  accepts both silently. A single-branch school can never reach it.
"""
from __future__ import annotations

from django.db import transaction

from schools.vs_academics.exceptions import BranchScopeConflict

from ..exceptions import (
    NoBellSchedule,
    RoomBranchConflict,
    SlotPeriodNotTeaching,
    SlotPeriodWrongDay,
    TimetableSpansBranches,
)
from ..models import ClassTimetable, Period, PeriodType, PublishState, TimetableSlot
from .teachers import assert_is_teacher


def assert_period_usable(period, *, day_of_week):
    """A lesson goes in a teaching period, on a day that period runs."""
    if period.period_type != PeriodType.LESSON:
        raise SlotPeriodNotTeaching(
            f"{period.label} is a {period.get_period_type_display().lower()}, "
            f"so no lesson can be scheduled in it.",
            period_id=period.pk,
        )
    if period.day_of_week is not None and period.day_of_week != day_of_week:
        raise SlotPeriodWrongDay(
            f"{period.label} does not run on this day.",
            period_id=period.pk,
        )


def assert_branches_agree(*, school_class, period, room, session, exclude_pk=None):
    """The three branch rules, in the order a school meets them."""
    class_branch = school_class.branch_id
    room_branch = room.branch_id if room is not None else None

    if room is not None and class_branch is not None and room_branch != class_branch:
        raise RoomBranchConflict(
            f"{room.name} is at another branch, so {school_class.name} cannot "
            f"be scheduled into it.",
            room=room.name,
            school_class=school_class.name,
        )

    if period.branch_id is not None:
        # The period rings at one branch. The lesson has to be there.
        where = room_branch if room is not None else class_branch
        if where is not None and period.branch_id != where:
            raise BranchScopeConflict(
                f"{period.label} is another branch's period, so this lesson "
                f"cannot be scheduled against it.",
                parent=period.label,
                parent_branch=None,
                given_branch=None,
            )

    if room is not None and class_branch is None:
        # A school-wide class. Its week may still only happen in one place.
        existing = (
            TimetableSlot.objects.filter(
                session=session, school_class=school_class, room__isnull=False,
            )
            .exclude(pk=exclude_pk)
            .exclude(room__branch_id=room_branch)
            .select_related("room__branch")
            .first()
        )
        if existing is not None:
            raise TimetableSpansBranches(
                f"{school_class.name} already has lessons at "
                f"{existing.room.branch.name}. A class's week happens at one "
                f"branch, because the pupils cannot travel between periods.",
                school_class=school_class.name,
                existing_branch=existing.room.branch.name,
            )


def validate_slot(tenant, session, *, school_class, day_of_week, period,
                  subject, teacher, room, exclude_pk=None):
    """Every rule a slot must satisfy before it is written."""
    # Belt and braces on ownership. TenantAwareManager scopes the related
    # fields eagerly, so a foreign row should never resolve - but the manager
    # is bypassed by all_objects and by related traversal, and a slot pointing
    # at another school's subject is the kind of thing that only shows up on a
    # timetable somebody prints.
    _assert_owned(tenant, subject, "subject")
    _assert_owned(tenant, period, "period")
    if room is not None:
        _assert_owned(tenant, room, "room")

    assert_period_usable(period, day_of_week=day_of_week)
    assert_is_teacher(tenant, teacher)
    assert_branches_agree(
        school_class=school_class, period=period, room=room, session=session,
        exclude_pk=exclude_pk,
    )


def require_bell_schedule(tenant, session, *, branch=None):
    """A grid is built on periods, so refuse to open one before they exist."""
    rows = Period.objects.filter(tenant=tenant, session=session, is_active=True)
    if not rows.exists():
        raise NoBellSchedule()


def timetable_for(tenant, session, school_class, *, create=False, actor=None):
    """The class's publication record, or None when it has never been started.

    Absent means "Not started", which is the design's third status and is
    otherwise unrepresentable - it is why this is a nullable lookup rather than
    a get_or_create everywhere.
    """
    row = ClassTimetable.objects.filter(
        session=session, school_class=school_class,
    ).first()
    if row is None and create:
        row = ClassTimetable.objects.create(
            tenant=tenant, session=session, school_class=school_class,
            created_by=actor,
        )
    return row


def touch_timetable(tenant, session, school_class, *, actor=None):
    """Record that this grid has been edited.

    **Editing a published grid returns it to draft**, because what was approved
    has changed. The design says so and the FRD records the absence of this rule
    as a real gap - "a published class timetable may still be edited and simply
    stops matching what was published", its decision 18. This closes that half:
    a school that edits is a school that has to publish again.
    """
    row = timetable_for(tenant, session, school_class, create=True, actor=actor)
    if row.status == PublishState.PUBLISHED:
        row.status = PublishState.DRAFT
        row.published_at = None
        row.save(update_fields=["status", "published_at", "updated_at"])
    return row


@transaction.atomic
def duplicate_grid(tenant, session, *, source_class, target_class, actor,
                   keep_teachers=True, keep_rooms=True, preview=False):
    """Copy one class's week into another's.

    **Replaces rather than merges**, and marks the target a draft. A half-copied
    grid is harder to reason about than a replaced one, and a copied grid is
    unapproved by definition.

    A source lesson sitting in a period the target does not run is **skipped and
    reported**, not silently dropped: a branch can run its own periods, so a
    Lekki Period 6 has no home in an Ikeja week that ends at Period 5.

    Copying without teachers or rooms is allowed and produces slots with gaps in
    them. That is the only way such a slot can exist, and it is why the publish
    gate checks completeness separately from clashes.
    """
    source_rows = list(
        TimetableSlot.objects.filter(session=session, school_class=source_class)
        .select_related("period", "subject", "teacher", "room"),
    )

    target_branch = target_class.branch_id
    usable, skipped = [], []
    for row in source_rows:
        period = row.period
        runs_here = (
            period.branch_id is None
            or target_branch is None
            or period.branch_id == target_branch
        )
        if not runs_here:
            skipped.append(row)
            continue
        usable.append(row)

    summary = {
        "source_class": source_class.name,
        "target_class": target_class.name,
        "copied": len(usable),
        "skipped": len(skipped),
        "replaced": TimetableSlot.objects.filter(
            session=session, school_class=target_class,
        ).count(),
        "rows": [
            {
                "day_of_week": row.day_of_week,
                "period": row.period.label,
                "subject": row.subject.name,
                "teacher": (
                    row.teacher_id and keep_teachers
                    and _name(row.teacher) or "No teacher"
                ),
                "room": (
                    row.room.name if (row.room_id and keep_rooms) else "No room"
                ),
            }
            for row in usable
        ],
        "skipped_rows": [
            {
                "day_of_week": row.day_of_week,
                "period": row.period.label,
                "subject": row.subject.name,
            }
            for row in skipped
        ],
    }
    if preview:
        return summary

    TimetableSlot.objects.filter(session=session, school_class=target_class).delete()
    TimetableSlot.objects.bulk_create([
        TimetableSlot(
            tenant=tenant, session=session, school_class=target_class,
            day_of_week=row.day_of_week, period=row.period, subject=row.subject,
            teacher=row.teacher if keep_teachers else None,
            room=row.room if keep_rooms else None,
            created_by=actor,
        )
        for row in usable
    ])

    record = timetable_for(tenant, session, target_class, create=True, actor=actor)
    if record.status != PublishState.DRAFT:
        record.status = PublishState.DRAFT
        record.published_at = None
        record.save(update_fields=["status", "published_at", "updated_at"])
    return summary


def _name(user):
    from .teachers import display_name

    return display_name(user)


def _assert_owned(tenant, row, label):
    from rest_framework.exceptions import NotFound

    if row is not None and getattr(row, "tenant_id", None) != tenant.id:
        # 404, never 403: a foreign id must not be distinguishable from one
        # that does not exist.
        raise NotFound(f"No such {label} at this school.")
