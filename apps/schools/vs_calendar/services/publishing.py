"""Moving a timetable from draft to published, and the one refusal that matters.

**This is the module's only hard refusal on a clash**, and putting it here
rather than on every write is a product decision worth stating so that it is not
undone. A school builds a grid over several sittings and must be able to save a
state it knows is wrong; publication is the one moment it asserts the grid is
finished, which is exactly where a correctness check belongs.

**The gate recomputes rather than reading a stored flag.** A clash is a
relationship between two rows, and editing either of them can create or resolve
one in a slot nobody touched - including one at another branch. A cached flag
would be a cache with no invalidation.

**The gate sees everything; the caller does not.** It runs with whole-tenant
visibility so it can never approve a schedule holding a clash the caller was not
shown, and the refusal it raises is then worded under the same redaction rule
the warnings use. So an Ikeja branch admin can be blocked by a clash whose other
half they are never told the details of. That asymmetry is deliberate: the block
is a fact about the school's timetable, and the detail is a disclosure decision.

**Two refusals, in this order.** Incompleteness first, then clashes, because a
lesson with no teacher is the more actionable message and a school that sees it
first fixes the right thing. Incompleteness is only reachable by duplicating
another class's week without its teachers or rooms - nothing else can write a
slot with a gap in it - which is why it is checked at the gate rather than at
the write.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_rbac.scoping import WHOLE_TENANT

from ..exceptions import TimetableHasClashes, TimetableIncomplete
from ..models import DayOfWeek, ExamSlot, PublishState, TimetableSlot
from .clashes import exam_clashes, grid_clashes
from .scoping import can_see_branch
from .timetable import timetable_for


def incomplete_lessons(session, school_class):
    """Slots with no teacher, no room, or neither. Named, not counted."""
    rows = (
        TimetableSlot.objects.filter(session=session, school_class=school_class)
        .filter(teacher__isnull=True)
        .union(
            TimetableSlot.objects.filter(
                session=session, school_class=school_class, room__isnull=True,
            ),
        )
    )
    # `union` cannot be select_related, so the ids are re-read in one query.
    ids = [row.pk for row in rows]
    if not ids:
        return []
    return list(
        TimetableSlot.objects.filter(pk__in=ids)
        .select_related("period", "subject")
        .order_by("day_of_week", "period__order_index"),
    )


def _gap_label(slot) -> str:
    if slot.teacher_id is None and slot.room_id is None:
        missing = "no teacher or room"
    elif slot.teacher_id is None:
        missing = "no teacher"
    else:
        missing = "no room"
    return (
        f"{DayOfWeek(slot.day_of_week).label} {slot.period.label} - "
        f"{slot.subject.name} has {missing}."
    )


@transaction.atomic
def publish_class_timetable(tenant, session, school_class, *, actor, visible):
    """Publish one class's week, or refuse and say why."""
    gaps = incomplete_lessons(session, school_class)
    if gaps:
        count = len(gaps)
        raise TimetableIncomplete(
            f"{count} {'lesson has' if count == 1 else 'lessons have'} no "
            f"teacher or room yet. Fill them in and publish again.",
            items=[_gap_label(slot) for slot in gaps],
            slot_ids=[slot.pk for slot in gaps],
        )

    # Whole-tenant, so a cross-branch clash can never slip past the gate.
    every = grid_clashes(tenant, session, school_class, visible=WHOLE_TENANT)
    if every:
        # Re-word for this caller: the block is not negotiable, the detail is.
        shown = grid_clashes(tenant, session, school_class, visible=visible)
        count = len(every)
        raise TimetableHasClashes(
            f"{count} {'clash is' if count == 1 else 'clashes are'} "
            f"unresolved. Fix {'it' if count == 1 else 'them'} and publish "
            f"again.",
            items=[w.detail for w in shown],
            slot_ids=sorted({pk for w in shown for pk in w.slot_ids}),
        )

    record = timetable_for(tenant, session, school_class, create=True, actor=actor)
    record.status = PublishState.PUBLISHED
    record.published_at = timezone.now()
    record.save(update_fields=["status", "published_at", "updated_at"])
    return record


@transaction.atomic
def publish_exam(tenant, exam, *, actor, visible):
    """Publish an exam timetable, or refuse and say why.

    Note which clashes block: a room used twice and an invigilator in two rooms
    both warn on write and both block here. A class sitting two papers at once
    never reaches this, because the unique constraint refused it at the write.
    """
    every = exam_clashes(tenant, exam, visible=WHOLE_TENANT)
    if every:
        shown = exam_clashes(tenant, exam, visible=visible)
        count = len(every)
        raise TimetableHasClashes(
            f"{count} {'clash is' if count == 1 else 'clashes are'} "
            f"unresolved. Fix {'it' if count == 1 else 'them'} and publish "
            f"again.",
            items=[w.detail for w in shown],
            slot_ids=sorted({pk for w in shown for pk in w.slot_ids}),
        )

    exam.status = PublishState.PUBLISHED
    exam.published_at = timezone.now()
    exam.save(update_fields=["status", "published_at", "updated_at"])
    return exam
