"""Taking a lesson register: marking, filling in the rest, and submitting.

A lesson register has the lifecycle of a class register (:mod:`.marks`) and is
simpler in three ways: it takes Present, Late or Absent only, it carries no
reason code, and it sends nothing to anybody, ever. No guardian is emailed, no
teacher is reminded and no notification is written, whatever the marks say.

**The register is created by the first mark,** under a row lock, with the lesson
copied onto it as the timetable gave it that day (:class:`~..models.LessonRegister`).

**Who may write, and until when.**

* Before submission, the lesson's teacher holding ``attendance.register.mark``,
  or a holder of ``attendance.register.mark_any``, marks freely. Nothing is
  recorded as a change: a draft is a draft.
* After submission and while the edit window is open
  (``attendance.edit_window_days``, 0 meaning the same day), only the person who
  submitted it, or a holder of ``mark_any``, can change a row. Every change is
  kept as a :class:`~..models.LessonMarkChange` and audited.
* Once the window has closed, **a submitted lesson mark is frozen**. This is
  deliberate: there is no correction request for a lesson, because a lesson
  mark is never an official record of absence from school and never reaches a
  family. A change is refused with ``LESSON_DAY_CLOSED``. A register never
  submitted can still be taken late, as a class register can.

**Rows are validated against the mark.** A Late mark needs an arrival time and
carries it; any other mark carries none. A note that is not sent is kept as it
was.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from vs_config.display import format_date

from ..constants import LESSON_MARKS, Mark
from ..exceptions import (
    AlreadySubmitted,
    ArrivalTimeRequired,
    EmptyRoster,
    LessonDayClosed,
    OnlySubmitterMayChange,
    StudentSuspended,
    UnmarkedRows,
)
from ..models import LessonMark, LessonMarkChange, LessonRegister
from . import audit
from .access import assert_may_mark_lesson
from .lessons import LessonContext
from .marks import UNSET, assert_register_is_due
from .registers import window_is_open

LESSON_MARK_VALUES = tuple(mark.value for mark in LESSON_MARKS)


def _lock_register(ctx: LessonContext) -> LessonRegister:
    """The register row, created if the first mark is being written, and locked."""
    slot = ctx.slot
    register, _ = LessonRegister.objects.select_for_update().get_or_create(
        tenant=ctx.tenant, school_class=ctx.school_class, period_id=slot.period_id,
        date=ctx.date,
        defaults={
            "slot": slot,
            "branch": ctx.branch,
            "session": ctx.school_class.session,
            "term": ctx.day.term,
            "period_label": slot.period.label,
            "starts_at": slot.period.start_time,
            "ends_at": slot.period.end_time,
            "subject": slot.subject,
            "teacher": slot.teacher,
        },
    )
    if register.slot_id != slot.pk:
        # A grid save replaces a class's slots: point at the one carrying the lesson now.
        register.slot = slot
        register.save(update_fields=["slot", "updated_at"])
    ctx.register = register
    return register


def _assert_changeable(ctx: LessonContext, register, user, is_any: bool) -> None:
    """Refuse a change to a submitted register the caller may no longer make."""
    if not register.is_submitted:
        return
    if not window_is_open(ctx):
        raise LessonDayClosed()
    if not (is_any or register.submitted_by_id == user.pk):
        raise OnlySubmitterMayChange()


def _entry(ctx: LessonContext, student_id):
    for entry in ctx.roster:
        if entry.student.pk == student_id:
            return entry
    raise NotFound("That student is not on this register.")


def _label(ctx: LessonContext) -> str:
    return (
        f"{ctx.school_class.name} {ctx.period_label} {ctx.subject.name}, "
        f"{format_date(ctx.date, ctx.tenant, branch=ctx.branch)}"
    )


def _audit(ctx: LessonContext, register, user, summary, **metadata) -> None:
    audit.record(
        tenant=ctx.tenant, user=user, branch=ctx.branch,
        entity_type="LessonRegister", entity_id=register.pk,
        entity_label=_label(ctx), summary=summary, **metadata,
    )


def _resolve_details(existing, mark, arrived_at, note):
    """The arrival time and note the row will hold, or a refusal."""
    if mark not in LESSON_MARK_VALUES:
        raise ValidationError({
            "mark": "Choose Present, Late or Absent. A lesson register has no Excused.",
        })
    if note is UNSET:
        note_final = existing.note if existing is not None else ""
    else:
        note_final = (note or "").strip()
    arrived = None
    if mark == Mark.LATE:
        if arrived_at is None:
            raise ArrivalTimeRequired(field="arrived_at")
        arrived = arrived_at.replace(second=0, microsecond=0)
    return arrived, note_final


@transaction.atomic
def set_mark(
    ctx: LessonContext, user, student_id, *, mark, arrived_at=None, note=UNSET,
) -> LessonMark:
    """Record *mark* for one student, replacing what the row held.

    Returns the saved row. A write that changes nothing writes nothing and
    records no change.
    """
    assert_register_is_due(ctx)
    is_any = assert_may_mark_lesson(user, ctx.tenant, ctx.slot, ctx.school_class)
    entry = _entry(ctx, student_id)
    if entry.suspended:
        raise StudentSuspended(
            f"{entry.student.first_name} was suspended on "
            f"{format_date(ctx.date, ctx.tenant, branch=ctx.branch)}, so cannot be marked.",
        )

    register = _lock_register(ctx)
    _assert_changeable(ctx, register, user, is_any)

    existing = (
        LessonMark.objects.select_for_update(of=("self",))
        .filter(register=register, student_id=student_id).first()
    )
    arrived, note_final = _resolve_details(existing, mark, arrived_at, note)
    if existing is not None and (
        existing.mark == mark and existing.arrived_at == arrived and existing.note == note_final
    ):
        return existing

    now = timezone.now()
    previous = existing.mark if existing is not None else ""
    row = existing or LessonMark(tenant=ctx.tenant, register=register, student=entry.student)
    row.mark = mark
    row.arrived_at = arrived
    row.note = note_final
    row.marked_by = user
    row.marked_at = now
    row.save()

    if register.is_submitted:
        LessonMarkChange.objects.create(
            tenant=ctx.tenant, mark=row, from_mark=previous, to_mark=row.mark,
            changed_by=user, changed_at=now, note=row.note,
        )
        was = Mark(previous).label if previous else "unmarked"
        _audit(
            ctx, register, user,
            f"{entry.student.full_name} changed from {was} to {Mark(row.mark).label} "
            f"in {ctx.school_class.name}'s {ctx.period_label} {ctx.subject.name} register.",
            student_id=row.student_id, from_mark=previous, to_mark=row.mark,
        )
    return row


@transaction.atomic
def fill_present(ctx: LessonContext, user) -> int:
    """Mark every unmarked, unlocked row Present. Submits nothing.

    Returns how many rows were filled. A register with nothing to fill is not
    created by asking.
    """
    assert_register_is_due(ctx)
    is_any = assert_may_mark_lesson(user, ctx.tenant, ctx.slot, ctx.school_class)

    marked = set()
    if ctx.register is not None:
        marked = set(
            LessonMark.objects.filter(register=ctx.register).values_list("student_id", flat=True),
        )
    if not [e for e in ctx.roster if not e.suspended and e.student.pk not in marked]:
        return 0

    register = _lock_register(ctx)
    _assert_changeable(ctx, register, user, is_any)
    marked = set(
        LessonMark.objects.filter(register=register).values_list("student_id", flat=True),
    )
    todo = [e for e in ctx.roster if not e.suspended and e.student.pk not in marked]
    now = timezone.now()
    rows = LessonMark.all_objects.bulk_create([
        LessonMark(
            tenant=ctx.tenant, register=register, student=e.student,
            mark=Mark.PRESENT, marked_by=user, marked_at=now,
        )
        for e in todo
    ])
    if register.is_submitted and rows:
        LessonMarkChange.all_objects.bulk_create([
            LessonMarkChange(
                tenant=ctx.tenant, mark=row, from_mark="", to_mark=Mark.PRESENT,
                changed_by=user, changed_at=now,
            )
            for row in rows
        ])
        _audit(
            ctx, register, user,
            f"{len(rows)} unmarked students were marked Present in "
            f"{ctx.school_class.name}'s {ctx.period_label} {ctx.subject.name} register.",
            filled=len(rows),
        )
    return len(rows)


@transaction.atomic
def submit(ctx: LessonContext, user) -> LessonRegister:
    """Submit the lesson register: record who and when. Nobody is told.

    Refused while any unlocked row is unmarked.
    """
    assert_register_is_due(ctx)
    assert_may_mark_lesson(user, ctx.tenant, ctx.slot, ctx.school_class)
    if not ctx.roster:
        raise EmptyRoster()

    register = (
        LessonRegister.objects.select_for_update(of=("self",))
        .filter(
            tenant=ctx.tenant, school_class=ctx.school_class,
            period_id=ctx.slot.period_id, date=ctx.date,
        )
        .select_related("submitted_by").first()
    )
    if register is not None and register.is_submitted:
        raise AlreadySubmitted(
            f"This register was already submitted by {register.submitted_by.full_name}."
            if register.submitted_by is not None else "",
        )

    marks = (
        list(LessonMark.objects.select_for_update().filter(register=register))
        if register is not None else []
    )
    marked = {m.student_id for m in marks}
    unlocked = [e for e in ctx.roster if not e.suspended]
    unmarked = [e for e in unlocked if e.student.pk not in marked]
    if unmarked:
        raise UnmarkedRows(
            f"Still {len(unmarked)} unmarked. A register cannot be submitted with an unmarked row.",
            unmarked=len(unmarked),
        )

    if register is None:
        # Every unlocked row was marked, vacuously: only locked rows are left.
        register = _lock_register(ctx)

    register.submitted_at = timezone.now()
    register.submitted_by = user
    register.save(update_fields=["submitted_at", "submitted_by", "updated_at"])
    ctx.register = register

    unlocked_ids = {e.student.pk for e in unlocked}
    tally = {code: 0 for code in LESSON_MARK_VALUES}
    for m in marks:
        if m.student_id in unlocked_ids:
            tally[m.mark] += 1
    _audit(
        ctx, register, user,
        f"{_label(ctx)} was submitted: {tally[Mark.PRESENT]} present, "
        f"{tally[Mark.LATE]} late, {tally[Mark.ABSENT]} absent.",
        present=tally[Mark.PRESENT], late=tally[Mark.LATE], absent=tally[Mark.ABSENT],
    )
    return register
