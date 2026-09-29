"""Moving a timetable from draft to published, and the one refusal that matters.

**This is the module's only hard refusal on a clash**, for a class timetable,
and putting it here
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

**Three refusals, in this order.** Incompleteness first, then teaching
duties, then clashes, because a lesson with no teacher is the most actionable
message and a school that sees it first fixes the right thing, and both of the
first two are about this class alone. A lesson with no teacher or no room is an
ordinary saved state: the lesson form, the grid save and a duplicate made
without teachers or rooms all write one, because a school fills the subjects of
a week before it fills the people and the places. That is why completeness is
checked at the gate rather than at the write.

**What counts as complete is partly the school's.** A lesson always needs a
teacher. It needs a room only while ``timetable.room_required_to_publish`` is
true, the default; a school that teaches every class in its own classroom turns
it off and publishes lessons with no room. A lesson whose teacher holds no
teaching duty for it refuses publication under ``timetable.teacher_duty_match``
REFUSE, and under WARN publishes and is listed in the answer's ``warnings``
(see ``services.duties``).

**An exam timetable is not gated.** Its only impossible clash, a class sitting
two papers at once, is refused at the write; the rest are things a school does
on purpose. See publish_exam.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_rbac.scoping import WHOLE_TENANT

from django.db.models import Q

from ..constants import CFG_ROOM_REQUIRED_TO_PUBLISH, CFG_TEACHER_DUTY_MATCH, DutyMatch
from ..exceptions import TimetableHasClashes, TimetableHasDutyGaps, TimetableIncomplete
from ..models import DayOfWeek, PublishState, TimetableSlot
from .calendar_rules import read_calendar_rules
from .clashes import grid_clashes
from .duties import duty_warnings, lessons_without_duty, no_duty_sentence
from .timetable import timetable_for


def incomplete_lessons(session, school_class, *, room_required=True):
    """Slots with no teacher, or no room when a room is required. Named, not counted."""
    gap = Q(teacher__isnull=True)
    if room_required:
        gap |= Q(room__isnull=True)
    return list(
        TimetableSlot.objects.filter(session=session, school_class=school_class)
        .filter(gap)
        .select_related("period", "subject")
        .order_by("day_of_week", "period__order_index"),
    )


def _where(slot) -> str:
    return (
        f"{DayOfWeek(slot.day_of_week).label} {slot.period.label} - "
        f"{slot.subject.name}"
    )


def _gap_label(slot, *, room_required=True) -> str:
    no_room = room_required and slot.room_id is None
    if slot.teacher_id is None and no_room:
        missing = "no teacher or room"
    elif slot.teacher_id is None:
        missing = "no teacher"
    else:
        missing = "no room"
    return f"{_where(slot)} has {missing}."


@transaction.atomic
def publish_class_timetable(tenant, session, school_class, *, actor, visible):
    """Publish one class's week, or refuse and say why.

    Answers ``(record, warnings)``: the publication record, and the
    ``TEACHER_HAS_NO_DUTY`` warnings of the lessons published under WARN
    (empty otherwise).
    """
    rules = read_calendar_rules(
        tenant, (CFG_ROOM_REQUIRED_TO_PUBLISH, CFG_TEACHER_DUTY_MATCH),
    )
    room_required = rules.room_required_to_publish
    gaps = incomplete_lessons(session, school_class, room_required=room_required)
    if gaps:
        count = len(gaps)
        missing = "teacher or room" if room_required else "teacher"
        raise TimetableIncomplete(
            f"{count} {'lesson has' if count == 1 else 'lessons have'} no "
            f"{missing} yet. Fill {'it' if count == 1 else 'them'} in and "
            f"publish again.",
            items=[_gap_label(slot, room_required=room_required) for slot in gaps],
            slot_ids=[slot.pk for slot in gaps],
        )

    mode = rules.teacher_duty_match
    lessons = []
    if mode != DutyMatch.OFF:
        lessons = list(
            TimetableSlot.objects.filter(session=session, school_class=school_class)
            .select_related("period", "subject", "teacher", "school_class")
            .order_by("day_of_week", "period__order_index"),
        )
    if mode == DutyMatch.REFUSE:
        missing = lessons_without_duty(tenant, session, lessons)
        if missing:
            count = len(missing)
            raise TimetableHasDutyGaps(
                f"{count} {'lesson has' if count == 1 else 'lessons have'} a "
                f"teacher with no teaching duty for "
                f"{'it' if count == 1 else 'them'}. Give the "
                f"{'duty' if count == 1 else 'duties'} in Teaching duties, or "
                f"change the {'teacher' if count == 1 else 'teachers'}, and "
                f"publish again.",
                items=[
                    f"{_where(slot)}: "
                    f"{no_duty_sentence(slot.teacher, slot.school_class, slot.subject)}"
                    for slot in missing
                ],
                slot_ids=[slot.pk for slot in missing],
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
    return record, duty_warnings(tenant, session, lessons, mode=mode)


@transaction.atomic
def publish_exam(tenant, exam, *, actor, visible):
    """Publish an exam timetable.

    Nothing that reaches here blocks it. A class sitting two papers at once is
    refused at the write by the unique constraint, because it is physically
    impossible. The other two clashes, a room holding several classes' papers
    and one invigilator between two rooms, are what a school legitimately does
    (the Main Hall seats JSS1 to JSS3 together), and the editor already warned
    about each one as it was written. Refusing them here refused the same thing
    on the same guess, with no way to say "we mean it", so a school with one
    hall could never publish. See exam_slot_warnings.
    """
    exam.status = PublishState.PUBLISHED
    exam.published_at = timezone.now()
    exam.save(update_fields=["status", "published_at", "updated_at"])
    return exam
