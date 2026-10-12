"""Taking a staff register: marking, filling in the rest, and submitting.

A staff register has the lifecycle of a class register (:mod:`.marks`) and is
simpler in three ways: it takes Present, Late or Absent only, it carries no
reason code, and it sends nothing to anybody, ever. No notification is written
and no email is sent, whatever the marks say.

**The register is created by the first mark,** under a row lock, so two
administrators opening the same branch's day at once cannot create two.

**Who may write, and until when.**

* The key ``attendance.staff.mark`` is the only gate on who: everybody who may
  mark staff may mark any member of staff in a branch they can open, so there is
  no "my own" rule and no difference between the person who submitted the
  register and anyone else holding the key.
* Before submission, marks are free. Nothing is recorded as a change: a draft is
  a draft.
* After submission and while the edit window is open
  (``attendance.edit_window_days``, 0 meaning the same day), a row changes in
  place. Every change is kept as a :class:`~..models.StaffMarkChange` and audited.
* Once the window has closed, **a submitted staff mark is frozen**. This is
  deliberate: there is no correction request for staff, because the register is
  a record of what an administrator saw, not a claim a family or payroll rests
  on. A change is refused with ``STAFF_DAY_CLOSED``. A register never submitted
  can still be taken late.

**Rows are validated against the mark.** A Late mark needs an arrival time and
carries it; any other mark carries none. A note that is not sent is kept as it was.

**One mark per person per day** is the database's rule (a unique constraint over
``(staff, date)``). A person whose home branch changed after they were marked
keeps the one mark they have: writing it again on the register they now belong
to moves it there, and submitting that register adopts it.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_config.display import format_date

from ..constants import STAFF_MARKS, Mark, StaffLock
from ..exceptions import (
    AlreadySubmitted,
    ArrivalTimeRequired,
    EmptyStaffRegister,
    StaffDayClosed,
    UnmarkedRows,
)
from ..models import StaffDay, StaffMark, StaffMarkChange
from . import audit
from .marks import UNSET
from .rules import read_edit_window_days
from .staff_register import (
    StaffContext,
    assert_register_is_due,
    edit_window_open,
    writable_entry,
)

STAFF_MARK_VALUES = tuple(mark.value for mark in STAFF_MARKS)


def _lock_day(ctx: StaffContext) -> StaffDay:
    """The day's register row, created if the first mark is being written, and locked."""
    staff_day, _ = StaffDay.objects.select_for_update().get_or_create(
        tenant=ctx.tenant, branch=ctx.branch, date=ctx.date,
    )
    ctx.staff_day = staff_day
    return staff_day


def _assert_changeable(ctx: StaffContext, staff_day: StaffDay) -> None:
    """Refuse a change to a submitted register whose edit window has closed."""
    if staff_day.is_submitted and not edit_window_open(
        ctx, read_edit_window_days(ctx.tenant),
    ):
        raise StaffDayClosed()


def _label(ctx: StaffContext) -> str:
    return f"{ctx.branch.name} staff, {format_date(ctx.date, ctx.tenant, branch=ctx.branch)}"


def _audit(ctx: StaffContext, staff_day, user, summary, **metadata) -> None:
    audit.record(
        tenant=ctx.tenant, user=user, branch=ctx.branch,
        entity_type="StaffDay", entity_id=staff_day.pk,
        entity_label=_label(ctx), summary=summary, **metadata,
    )


def _resolve_details(existing, mark, arrived_at, note):
    """The arrival time and note the row will hold, or a refusal."""
    if mark not in STAFF_MARK_VALUES:
        raise ValidationError({
            "mark": "Choose Present, Late or Absent. A member of staff away with "
                    "permission is on leave, which is filled in for you.",
        })
    if note is UNSET:
        note_final = existing.note if existing is not None else ""
    else:
        note_final = (note or "").strip()
    arrived = None
    if mark == Mark.LATE:
        if arrived_at is None:
            raise ArrivalTimeRequired("Say when they arrived.", field="arrived_at")
        arrived = arrived_at.replace(second=0, microsecond=0)
    return arrived, note_final


@transaction.atomic
def set_mark(
    ctx: StaffContext, user, staff_id, *, mark, arrived_at=None, note=UNSET,
) -> StaffMark:
    """Record *mark* for one member of staff, replacing what the row held.

    Returns the saved row. A write that changes nothing writes nothing and
    records no change.
    """
    assert_register_is_due(ctx)
    entry = writable_entry(ctx, staff_id)

    staff_day = _lock_day(ctx)
    _assert_changeable(ctx, staff_day)

    existing = (
        StaffMark.objects.select_for_update(of=("self",))
        .filter(tenant=ctx.tenant, staff_id=staff_id, date=ctx.date).first()
    )
    arrived, note_final = _resolve_details(existing, mark, arrived_at, note)
    if existing is not None and (
        existing.mark == mark and existing.arrived_at == arrived
        and existing.note == note_final and existing.staff_day_id == staff_day.pk
    ):
        return existing

    now = timezone.now()
    previous = existing.mark if existing is not None else ""
    row = existing or StaffMark(tenant=ctx.tenant, staff=entry.staff, date=ctx.date)
    row.staff_day = staff_day
    row.mark = mark
    row.arrived_at = arrived
    row.note = note_final
    row.marked_by = user
    row.marked_at = now
    row.save()

    if staff_day.is_submitted:
        StaffMarkChange.objects.create(
            tenant=ctx.tenant, mark=row, from_mark=previous, to_mark=row.mark,
            changed_by=user, changed_at=now, note=row.note,
        )
        was = Mark(previous).label if previous else "unmarked"
        _audit(
            ctx, staff_day, user,
            f"{entry.name} changed from {was} to {Mark(row.mark).label} on "
            f"{ctx.branch.name}'s staff register.",
            staff_id=entry.staff.pk, from_mark=previous, to_mark=row.mark,
        )
    return row


def _unmarked(ctx: StaffContext, taken: set) -> list:
    return [e for e in ctx.entries if e.lock is None and e.staff.pk not in taken]


@transaction.atomic
def fill_present(ctx: StaffContext, user) -> int:
    """Mark every unmarked, unlocked row Present. Submits nothing.

    Returns how many rows were filled. A register with nothing to fill is not
    created by asking.
    """
    assert_register_is_due(ctx)

    ids = [e.staff.pk for e in ctx.entries]

    def taken():
        return set(
            StaffMark.objects.filter(tenant=ctx.tenant, date=ctx.date, staff_id__in=ids)
            .values_list("staff_id", flat=True),
        )

    if not _unmarked(ctx, taken()):
        return 0

    staff_day = _lock_day(ctx)
    _assert_changeable(ctx, staff_day)
    todo = _unmarked(ctx, taken())
    now = timezone.now()
    rows = StaffMark.all_objects.bulk_create([
        StaffMark(
            tenant=ctx.tenant, staff_day=staff_day, staff=e.staff, date=ctx.date,
            mark=Mark.PRESENT, marked_by=user, marked_at=now,
        )
        for e in todo
    ])
    if staff_day.is_submitted and rows:
        StaffMarkChange.all_objects.bulk_create([
            StaffMarkChange(
                tenant=ctx.tenant, mark=row, from_mark="", to_mark=Mark.PRESENT,
                changed_by=user, changed_at=now,
            )
            for row in rows
        ])
        _audit(
            ctx, staff_day, user,
            f"{len(rows)} unmarked staff were marked Present on "
            f"{ctx.branch.name}'s staff register.",
            filled=len(rows),
        )
    return len(rows)


@transaction.atomic
def submit(ctx: StaffContext, user) -> StaffDay:
    """Submit the staff register: record who and when. Nobody is told.

    Refused while any unlocked row is unmarked, and when nobody is on it.
    """
    assert_register_is_due(ctx)
    if not ctx.entries:
        raise EmptyStaffRegister()

    staff_day = (
        StaffDay.objects.select_for_update(of=("self",))
        .filter(tenant=ctx.tenant, branch=ctx.branch, date=ctx.date)
        .select_related("submitted_by").first()
    )
    if staff_day is not None and staff_day.is_submitted:
        raise AlreadySubmitted(
            f"This register was already submitted by {staff_day.submitted_by.full_name}."
            if staff_day.submitted_by is not None else "",
        )

    ids = [e.staff.pk for e in ctx.entries]
    marks = list(
        StaffMark.objects.select_for_update()
        .filter(tenant=ctx.tenant, date=ctx.date, staff_id__in=ids),
    )
    unmarked = _unmarked(ctx, {m.staff_id for m in marks})
    if unmarked:
        raise UnmarkedRows(
            f"Still {len(unmarked)} unmarked. A register cannot be submitted with an unmarked row.",
            unmarked=len(unmarked),
        )

    if staff_day is None:
        # Every unlocked row was marked, vacuously: only locked rows are left.
        staff_day = _lock_day(ctx)

    staff_day.submitted_at = timezone.now()
    staff_day.submitted_by = user
    staff_day.save(update_fields=["submitted_at", "submitted_by", "updated_at"])
    ctx.staff_day = staff_day
    StaffMark.all_objects.filter(
        pk__in=[m.pk for m in marks if m.staff_day_id != staff_day.pk],
    ).update(staff_day=staff_day)

    live_ids = {e.staff.pk for e in ctx.entries if e.lock is None}
    tally = {code: 0 for code in STAFF_MARK_VALUES}
    for mark in marks:
        if mark.staff_id in live_ids:
            tally[mark.mark] += 1
    on_leave = sum(1 for e in ctx.entries if e.lock == StaffLock.ON_LEAVE.value)
    _audit(
        ctx, staff_day, user,
        f"{_label(ctx)} was submitted: {tally[Mark.PRESENT]} present, "
        f"{tally[Mark.LATE]} late, {tally[Mark.ABSENT]} absent, {on_leave} on leave.",
        present=tally[Mark.PRESENT], late=tally[Mark.LATE], absent=tally[Mark.ABSENT],
        on_leave=on_leave,
    )
    return staff_day
