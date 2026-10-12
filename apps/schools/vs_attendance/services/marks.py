"""Taking a register: marking, filling in the rest, and submitting.

**The register is created by the first mark**, under a row lock, so two people
opening the same class's register at once cannot create two. Every write below
re-reads the register under that lock before it decides anything, because the
context a request resolved is a moment old by the time it writes.

**Who may write, and until when.**

* Before submission, anyone who may take the class's register
  (:func:`services.access.assert_may_take`) can mark freely. Nothing is
  recorded as a change: a draft is a draft.
* After submission and while the edit window is open
  (``attendance.edit_window_days``, 0 meaning the same day), only the person
  who submitted it, or a holder of ``attendance.register.mark_any``, can change
  a row. Every change is kept as a :class:`~..models.MarkChange` and audited.
* Once the window has closed, nobody changes a row here: the mark changes only
  through a correction request. A register never submitted can still be taken
  late, because a register nobody took is not a register nobody may take.

**Rows are validated against the mark.** A Late mark needs an arrival time. An
Excused mark needs one of the school's live reason codes, and a code that asks
for a note ("Other") needs one. A mark that is neither carries neither: they
are cleared rather than kept, so a row changed from Excused to Present does not
go on naming a reason. A note that is not sent is kept as it was.

**Guardian notices** follow the mark (see :mod:`.notices`): submitting emails
the guardians of the absent children, a same-day change away from an absence
whose notice went emails the correction, and a change to Absent after
submission notices the guardian as the submission would have.
"""
from __future__ import annotations

from functools import partial

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from vs_config.clock import branch_today
from vs_config.display import format_date

from ..constants import ChangeVia, Mark, NoticeState
from ..exceptions import (
    AlreadySubmitted,
    ArrivalTimeRequired,
    DateInFuture,
    DayClosedNoRegister,
    DayClosedUseCorrection,
    EmptyRoster,
    NoteRequired,
    OnlySubmitterMayChange,
    StudentSuspended,
    UnmarkedRows,
)
from ..models import ClassRegister, MarkChange, StudentMark
from . import audit, notices
from .access import assert_may_take
from .days import CODE_CLOSED_BY_EVENT, DayStatus
from .reasons import ensure_default_reason_codes, live_reason_code
from .registers import RegisterContext, window_is_open

#: A body field that was not sent, as distinct from one sent blank.
UNSET = object()


def closed_sentence(day: DayStatus) -> str:
    """Why no register is due on a closed day, in a sentence for the screen."""
    if day.code == CODE_CLOSED_BY_EVENT:
        return f"The school is closed that day ({day.reason}). There is no register to take."
    return f"{day.reason} There is no register to take."


def assert_register_is_due(ctx: RegisterContext) -> None:
    """Refuse a day that has not come, and a day the school is closed."""
    if ctx.date > branch_today(ctx.tenant, ctx.branch):
        raise DateInFuture()
    if not ctx.day.is_open:
        raise DayClosedNoRegister(closed_sentence(ctx.day))


def _lock_register(ctx: RegisterContext) -> ClassRegister:
    """The register row, created if the first mark is being written, and locked."""
    register, _ = ClassRegister.objects.select_for_update().get_or_create(
        tenant=ctx.tenant, school_class=ctx.school_class, date=ctx.date,
        defaults={
            "branch": ctx.branch,
            "session": ctx.school_class.session,
            "term": ctx.day.term,
        },
    )
    ctx.register = register
    return register


def _assert_changeable(ctx: RegisterContext, register, user, is_any: bool) -> None:
    """Refuse a change to a submitted register the caller may no longer make."""
    if not register.is_submitted:
        return
    if not window_is_open(ctx):
        raise DayClosedUseCorrection()
    if not (is_any or register.submitted_by_id == user.pk):
        raise OnlySubmitterMayChange()


def _entry(ctx: RegisterContext, student_id):
    for entry in ctx.roster:
        if entry.student.pk == student_id:
            return entry
    raise NotFound("That student is not on this register.")


def _audit(ctx, register, user, summary, **metadata) -> None:
    audit.record(
        tenant=ctx.tenant, user=user, branch=ctx.branch,
        entity_type="ClassRegister", entity_id=register.pk,
        entity_label=audit.register_label(ctx.school_class, ctx.date, ctx.tenant, ctx.branch),
        summary=summary, **metadata,
    )


def _resolve_details(ctx, existing, mark, arrived_at, reason_code_id, note):
    """The arrival time, reason code and note the row will hold, or a refusal."""
    if mark not in Mark.values:
        raise ValidationError({"mark": "Choose Present, Late, Absent or Excused."})

    if note is UNSET:
        note_final = existing.note if existing is not None else ""
    else:
        note_final = (note or "").strip()

    arrived = None
    code = None
    if mark == Mark.LATE:
        if arrived_at is None:
            raise ArrivalTimeRequired(field="arrived_at")
        arrived = arrived_at.replace(second=0, microsecond=0)
    elif mark == Mark.EXCUSED:
        if (
            existing is not None and existing.mark == Mark.EXCUSED
            and reason_code_id is not None and existing.reason_code_id == reason_code_id
        ):
            # A reason retired since is kept on the marks that used it, so
            # saving the same row again is not asking for it afresh.
            code = existing.reason_code
        else:
            code = live_reason_code(ctx.tenant, reason_code_id)
        if code.requires_note and not note_final:
            raise NoteRequired(
                f"{code.name} needs a note saying what happened.", field="note",
            )
    return arrived, code, note_final


@transaction.atomic
def set_mark(
    ctx: RegisterContext, user, student_id, *, mark, arrived_at=None,
    reason_code_id=None, note=UNSET,
) -> StudentMark:
    """Record *mark* for one student, replacing what the row held.

    Returns the saved row. A write that changes nothing writes nothing and
    records no change.
    """
    assert_register_is_due(ctx)
    is_any = assert_may_take(user, ctx.tenant, ctx.school_class)
    entry = _entry(ctx, student_id)
    if entry.suspended:
        raise StudentSuspended(
            f"{entry.student.first_name} was suspended on "
            f"{format_date(ctx.date, ctx.tenant, branch=ctx.branch)}, so cannot be marked.",
        )
    ensure_default_reason_codes(ctx.tenant)

    register = _lock_register(ctx)
    _assert_changeable(ctx, register, user, is_any)

    existing = (
        StudentMark.objects.select_for_update(of=("self",))
        .filter(register=register, student_id=student_id)
        .select_related("reason_code").first()
    )
    arrived, code, note_final = _resolve_details(
        ctx, existing, mark, arrived_at, reason_code_id, note,
    )
    if existing is not None and (
        existing.mark == mark and existing.arrived_at == arrived
        and existing.reason_code_id == (code.pk if code else None)
        and existing.note == note_final
    ):
        return existing

    now = timezone.now()
    previous = existing.mark if existing is not None else ""
    row = existing or StudentMark(tenant=ctx.tenant, register=register, student=entry.student)
    row.mark = mark
    row.arrived_at = arrived
    row.reason_code = code
    row.note = note_final
    row.marked_by = user
    row.marked_at = now
    if mark != Mark.ABSENT and row.notice_state != NoticeState.SENT:
        row.notice_state = NoticeState.NONE
    row.save()

    if register.is_submitted:
        _record_change(ctx, register, row, previous, user, now)
    return row


def _record_change(ctx, register, row, previous, user, now) -> None:
    """Keep, audit and notify one change to a submitted register."""
    MarkChange.objects.create(
        tenant=ctx.tenant, mark=row, from_mark=previous, to_mark=row.mark,
        changed_by=user, changed_at=now, via=ChangeVia.SAME_DAY, note=row.note,
    )
    name = row.student.full_name
    was = Mark(previous).label if previous else "unmarked"
    _audit(
        ctx, register, user,
        f"{name} changed from {was} to {Mark(row.mark).label} in "
        f"{ctx.school_class.name}'s register.",
        student_id=row.student_id, from_mark=previous, to_mark=row.mark,
    )
    if previous == Mark.ABSENT and row.mark != Mark.ABSENT:
        transaction.on_commit(partial(notices.send_correction_notice, row, row.mark))
    elif row.mark == Mark.ABSENT and previous != Mark.ABSENT:
        if row.notice_state != NoticeState.SENT:
            to_send = notices.decide_absence_notices(ctx.tenant, register, [row])
            if to_send:
                transaction.on_commit(
                    partial(notices.send_absence_notices, ctx.tenant, to_send),
                )


@transaction.atomic
def fill_present(ctx: RegisterContext, user) -> int:
    """Mark every unmarked, unlocked row Present. Submits nothing.

    Returns how many rows were filled. A register with nothing to fill is not
    created by asking.
    """
    assert_register_is_due(ctx)
    is_any = assert_may_take(user, ctx.tenant, ctx.school_class)

    marked = set()
    if ctx.register is not None:
        marked = set(
            StudentMark.objects.filter(register=ctx.register)
            .values_list("student_id", flat=True),
        )
    if not [e for e in ctx.roster if not e.suspended and e.student.pk not in marked]:
        return 0

    register = _lock_register(ctx)
    _assert_changeable(ctx, register, user, is_any)
    marked = set(
        StudentMark.objects.filter(register=register).values_list("student_id", flat=True),
    )
    todo = [e for e in ctx.roster if not e.suspended and e.student.pk not in marked]
    now = timezone.now()
    rows = StudentMark.all_objects.bulk_create([
        StudentMark(
            tenant=ctx.tenant, register=register, student=e.student,
            mark=Mark.PRESENT, marked_by=user, marked_at=now,
        )
        for e in todo
    ])
    if register.is_submitted and rows:
        MarkChange.all_objects.bulk_create([
            MarkChange(
                tenant=ctx.tenant, mark=row, from_mark="", to_mark=Mark.PRESENT,
                changed_by=user, changed_at=now, via=ChangeVia.SAME_DAY,
            )
            for row in rows
        ])
        _audit(
            ctx, register, user,
            f"{len(rows)} unmarked students were marked Present in "
            f"{ctx.school_class.name}'s register.",
            filled=len(rows),
        )
    return len(rows)


@transaction.atomic
def submit(ctx: RegisterContext, user) -> ClassRegister:
    """Submit the register: record who and when, then notify the families of the absent.

    Refused while any unlocked row is unmarked. The guardians' emails go out
    once the transaction has committed.
    """
    assert_register_is_due(ctx)
    assert_may_take(user, ctx.tenant, ctx.school_class)
    if not ctx.roster:
        raise EmptyRoster()

    register = (
        ClassRegister.objects.select_for_update(of=("self",))
        .filter(tenant=ctx.tenant, school_class=ctx.school_class, date=ctx.date)
        .select_related("submitted_by").first()
    )
    if register is not None and register.is_submitted:
        raise AlreadySubmitted(
            f"This register was already submitted by {register.submitted_by.full_name}."
            if register.submitted_by is not None else "",
        )

    marks = (
        list(StudentMark.objects.select_for_update().filter(register=register))
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

    teacher = ctx.school_class.class_teacher
    register.submitted_at = timezone.now()
    register.submitted_by = user
    register.on_behalf_of = teacher if teacher is not None and teacher.user_id != user.pk else None
    register.save(update_fields=["submitted_at", "submitted_by", "on_behalf_of", "updated_at"])
    ctx.register = register

    unlocked_ids = {e.student.pk for e in unlocked}
    absent = [m for m in marks if m.mark == Mark.ABSENT and m.student_id in unlocked_ids]
    to_send = notices.decide_absence_notices(ctx.tenant, register, absent)
    if to_send:
        transaction.on_commit(partial(notices.send_absence_notices, ctx.tenant, to_send))

    tally = {code: 0 for code in Mark.values}
    for m in marks:
        if m.student_id in unlocked_ids:
            tally[m.mark] += 1
    _audit(
        ctx, register, user,
        f"{ctx.school_class.name}'s register for "
        f"{format_date(ctx.date, ctx.tenant, branch=ctx.branch)} was submitted: "
        f"{tally[Mark.PRESENT]} present, {tally[Mark.LATE]} late, "
        f"{tally[Mark.ABSENT]} absent, {tally[Mark.EXCUSED]} excused.",
        present=tally[Mark.PRESENT], late=tally[Mark.LATE],
        absent=tally[Mark.ABSENT], excused=tally[Mark.EXCUSED],
    )
    return register
