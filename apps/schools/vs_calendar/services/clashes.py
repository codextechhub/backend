"""The three ways a timetable can contradict itself, and who is told what.

**Why this works when so much else does not.** A clash is a property of the
timetable rows, not of the teacher record. Two slots sharing a teacher id, a day
and a period are a clash whether that id points at a rich staff profile or at a
bare login, because the comparison never looks past the id. That is the whole
reason this half of the module is worth building on the person record that
exists, and it is why clash detection can be presented to a school as
trustworthy when nothing about a teacher's specialism, availability or workload
can be.

**Warn, never refuse.** A clash is recorded, persisted and returned in
``data.warnings`` beside the write that created it. A school builds a grid over
several sittings and needs to save a state it knows is wrong - the design shows
the clashing cells in red and says so in as many words: "The grid saves with
clashes in it. They only block publishing." The single refusal is at
publication, in ``services.publishing``, which is the one moment the school
asserts the grid is finished.

**The queries span the tenant, deliberately.** For teachers and for classes they
are NOT narrowed by ``visible_branch_ids``, and this is the one place in the
module where a query is wider than the caller's read scope. A person cannot be
at two branches at once however the school's permissions are arranged, and a
school-wide class is one row both branches' admins can see and schedule. If the
teacher query ran inside the caller's visible branches, the Ikeja admin would
schedule Mr Eze at 09:30 on Wednesday, be told nothing, and the school would
find out when a class of thirty sat in an empty room.

A room query needs no widening: a room belongs to exactly one branch by
construction, so two slots sharing a room already share a branch and the caller
can always see both sides.

``test_clashes.py`` asserts the width, so adding ``visible_branch_ids`` here is
a failing test rather than a silent product change.

**What a caller is told about a branch they cannot see.** The clash is always
reported; only the detail varies. Where the other slot is inside the caller's
visible branches the warning names the class, the day, the period and the room.
Where it is not, it names the day and the period and says the person is already
teaching at another branch - and names neither the class, nor the room, nor any
branch id, so the endpoint cannot be used to map another branch's grid.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..constants import (
    WARN_INVIGILATOR_DOUBLE_BOOKED,
    WARN_ROOM_DOUBLE_BOOKED,
    WARN_TEACHER_DOUBLE_BOOKED,
)
from ..models import DayOfWeek, ExamSlot, Sitting, TimetableSlot
from .scoping import can_see_branch
from .teachers import display_name

#: Sittings rank by time of day, never by name: "AFTERNOON" sorts before
#: "MORNING" lexically, which would invert every exam day holding both.
SITTING_RANK = {Sitting.MORNING: 0, Sitting.AFTERNOON: 1}


@dataclass
class Warning_:
    """One clash, as the API returns it: a code and a sentence for the screen."""

    code: str
    detail: str
    #: The slots involved, so the client can flag both cells. Only ever ids the
    #: caller may already see; a redacted warning carries its own slot alone.
    slot_ids: list = field(default_factory=list)

    def as_dict(self):
        return {"code": self.code, "detail": self.detail, "slot_ids": self.slot_ids}


def _day_label(day_of_week) -> str:
    return DayOfWeek(day_of_week).label


def _branch_of_slot(slot):
    """Where a slot happens: its room's branch, or its class's if it has no room.

    A slot carries no branch column of its own, deliberately - it would be a
    second answer to a question the room already answers, and free to
    contradict it.
    """
    if slot.room_id and slot.room is not None:
        return slot.room.branch_id
    return getattr(slot.school_class, "branch_id", None)


# ── Class timetable ────────────────────────────────────────────────────────

def slot_warnings(slot, *, visible, queryset=None):
    """Every clash *slot* is part of, worded for a caller who can see *visible*."""
    out = []
    others = _sibling_slots(slot, queryset=queryset)

    for other in others:
        if slot.teacher_id and other.teacher_id == slot.teacher_id:
            out.append(_teacher_warning(slot, other, visible=visible))
        if slot.room_id and other.room_id == slot.room_id:
            out.append(_room_warning(slot, other, visible=visible))
    return out


def _sibling_slots(slot, *, queryset=None):
    """Other slots in the same session, day and period. Tenant-wide."""
    rows = queryset if queryset is not None else TimetableSlot.objects.filter(
        tenant_id=slot.tenant_id,
        session_id=slot.session_id,
        day_of_week=slot.day_of_week,
        period_id=slot.period_id,
    ).select_related("school_class", "room", "teacher")
    return [row for row in rows if row.pk != slot.pk]


def _teacher_warning(slot, other, *, visible):
    who = display_name(slot.teacher)
    where = f"{_day_label(slot.day_of_week)} {slot.period.label}"
    if can_see_branch(visible, _branch_of_slot(other)):
        return Warning_(
            code=WARN_TEACHER_DOUBLE_BOOKED,
            detail=(
                f"{who} is double-booked (also {other.school_class.name}, "
                f"{where})."
            ),
            slot_ids=[slot.pk, other.pk],
        )
    return Warning_(
        code=WARN_TEACHER_DOUBLE_BOOKED,
        detail=f"{who} is already teaching at another branch ({where}).",
        # The other slot's id is withheld with its name: an id is enumerable.
        slot_ids=[slot.pk],
    )


def _room_warning(slot, other, *, visible):
    who = slot.room.name
    where = f"{_day_label(slot.day_of_week)} {slot.period.label}"
    if can_see_branch(visible, _branch_of_slot(other)):
        return Warning_(
            code=WARN_ROOM_DOUBLE_BOOKED,
            detail=(
                f"{who} is double-booked (also {other.school_class.name}, "
                f"{where})."
            ),
            slot_ids=[slot.pk, other.pk],
        )
    # Reachable only through a school-wide class scheduled into another
    # branch's room, which TIMETABLE_SPANS_BRANCHES refuses - kept so the
    # redaction rule has no hole in it.
    return Warning_(
        code=WARN_ROOM_DOUBLE_BOOKED,
        detail=f"{who} is already booked at another branch ({where}).",
        slot_ids=[slot.pk],
    )


def grid_clashes(tenant, session, school_class, *, visible):
    """Every clash in one class's week. Three queries, whatever the grid size.

    Used by the grid read and by the publish gate. The gate calls it with
    ``visible`` set to whole-tenant so that it can never approve a schedule
    holding a clash the caller was not shown; the read calls it with the
    caller's own scope.
    """
    mine = list(
        TimetableSlot.objects.filter(
            tenant=tenant, session=session, school_class=school_class,
        ).select_related("period", "room", "teacher", "school_class"),
    )
    if not mine:
        return []

    keys = {(s.day_of_week, s.period_id) for s in mine}
    others = list(
        TimetableSlot.objects.filter(
            tenant=tenant, session=session,
            day_of_week__in={k[0] for k in keys},
            period_id__in={k[1] for k in keys},
        )
        .exclude(school_class=school_class)
        .select_related("period", "room", "teacher", "school_class"),
    )

    by_key = {}
    for row in others:
        by_key.setdefault((row.day_of_week, row.period_id), []).append(row)

    out = []
    for slot in mine:
        siblings = by_key.get((slot.day_of_week, slot.period_id), [])
        out.extend(slot_warnings(slot, visible=visible, queryset=siblings))
    return out


# ── Exams ──────────────────────────────────────────────────────────────────

def exam_slot_warnings(slot, *, visible, queryset=None):
    """Room and invigilator clashes for one paper.

    A class sitting two papers in one sitting is not here: it is refused by the
    unique constraint, because it is physically impossible and a school never
    means it. The other two warn, because a school legitimately runs two
    classes' papers in the Main Hall at once and legitimately floats one
    invigilator between two adjacent rooms - and nothing in the platform records
    how many candidates a paper has or how many rooms a person can supervise, so
    refusing either would be refusing on a guess.
    """
    out = []
    others = queryset if queryset is not None else list(
        ExamSlot.objects.filter(
            tenant_id=slot.tenant_id, exam_id=slot.exam_id,
            exam_date=slot.exam_date, sitting=slot.sitting,
        )
        .exclude(pk=slot.pk)
        .select_related("school_class", "room", "invigilator"),
    )
    when = f"{slot.exam_date:%d %b %Y} {Sitting(slot.sitting).label.lower()} sitting"

    for other in others:
        if slot.room_id and other.room_id == slot.room_id:
            out.append(_exam_room_warning(slot, other, when=when, visible=visible))
        if (
            slot.invigilator_id
            and other.invigilator_id == slot.invigilator_id
            and other.room_id != slot.room_id
        ):
            out.append(
                _exam_invigilator_warning(slot, other, when=when, visible=visible),
            )
    return out


def _exam_room_warning(slot, other, *, when, visible):
    who = slot.room.name
    if can_see_branch(visible, getattr(other.school_class, "branch_id", None)):
        return Warning_(
            code=WARN_ROOM_DOUBLE_BOOKED,
            detail=f"{who} is also used by {other.school_class.name} in the {when}.",
            slot_ids=[slot.pk, other.pk],
        )
    return Warning_(
        code=WARN_ROOM_DOUBLE_BOOKED,
        detail=f"{who} is also used by a class at another branch in the {when}.",
        slot_ids=[slot.pk],
    )


def _exam_invigilator_warning(slot, other, *, when, visible):
    who = display_name(slot.invigilator)
    if can_see_branch(visible, getattr(other.school_class, "branch_id", None)):
        other_room = other.room.name if other.room_id else "another room"
        return Warning_(
            code=WARN_INVIGILATOR_DOUBLE_BOOKED,
            detail=f"{who} is already invigilating {other_room} in the {when}.",
            slot_ids=[slot.pk, other.pk],
        )
    return Warning_(
        code=WARN_INVIGILATOR_DOUBLE_BOOKED,
        detail=f"{who} is already invigilating at another branch ({when}).",
        slot_ids=[slot.pk],
    )


def exam_clashes(tenant, exam, *, visible):
    """Every clash in one exam schedule, deduplicated to one entry per problem.

    One entry per clash, not per participant: a room used by two classes is one
    problem to fix, so a pairwise walk would double both the list and the count.
    """
    rows = list(
        ExamSlot.objects.filter(tenant=tenant, exam=exam)
        .select_related("school_class", "room", "invigilator"),
    )
    by_sitting = {}
    for row in rows:
        by_sitting.setdefault((row.exam_date, row.sitting), []).append(row)

    seen = set()
    out = []
    for group in by_sitting.values():
        for slot in group:
            for warning in exam_slot_warnings(
                slot, visible=visible,
                queryset=[r for r in group if r.pk != slot.pk],
            ):
                key = (warning.code, tuple(sorted(warning.slot_ids)))
                if key in seen:
                    continue
                seen.add(key)
                out.append(warning)
    return out
