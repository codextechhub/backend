"""Correcting a mark after its day has closed.

**When a correction is the way.** While a register's edit window is open the
person who took it changes a row directly (:mod:`.marks`). Once the window has
closed the row is frozen, and the only way to change it is to ask: a teacher
or administrator files a :class:`~..models.CorrectionRequest` naming the mark
they want, why, and a note, and a holder of ``attendance.correction.approve``
decides it in one step. There is no workflow template: the decision is a
single approval by somebody other than the requester.

**One request at a time per mark.** A mark with a correction waiting takes no
second one, whatever it asks for, so an approver never has two requests that
contradict each other and the mark approved always still holds the value the
request was written against. The mark row is locked while a request is filed
and while it is decided, so two people filing at once, or two approvers
deciding at once, cannot both succeed. If the mark has moved since the request
was filed (the edit window was widened and the register changed in place) the
approval is refused as stale rather than applied over the newer mark.

**Approval changes the mark and keeps the old one.** The row takes the new mark
and its details, a :class:`~..models.MarkChange` records who moved it from what
to what, and the request keeps its decision. Rejection leaves the mark alone and
keeps the request and the decision on the student's record.

**Guardians.** Correcting an absence whose notice was emailed sends the
correction notice to the guardian who received it, once the transaction has
committed. Correcting a mark *to* Absent sends no absence notice, however the
school's notices are set: the day is over, the family has been told nothing
about it, and an email arriving days later announcing an absence nobody
reported at the time does more harm than the record it corrects.
"""
from __future__ import annotations

import datetime as dt
from functools import partial

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from vs_audit.models import AuditActionType
from vs_rbac.scoping import transaction_branch_q_for_user

from ..constants import ChangeVia, CorrectionStatus, Mark, NoticeState
from ..exceptions import (
    AlreadyDecided,
    ArrivalTimeRequired,
    CorrectionAlreadyPending,
    CorrectionNotNeeded,
    CorrectionStale,
    NoChange,
    NoteRequired,
    ReasonCodeRequired,
    SelfDecision,
)
from ..models import CorrectionRequest, MarkChange, StudentMark
from . import audit, notices
from .access import assert_may_request_correction
from .reasons import ensure_default_reason_codes, live_reason_code
from .registers import RegisterContext, window_is_open

#: What a row of the list or the hub needs from the database, in one query.
_ROW_RELATIONS = (
    "mark__student", "mark__register__school_class", "mark__register__branch",
    "reason_code", "requested_by", "decided_by",
)


def _label(code: str) -> str:
    return Mark(code).label if code else "unmarked"


def _assert_correction_is_the_way(ctx: RegisterContext) -> None:
    register = ctx.register
    if register is None or not register.is_submitted:
        raise CorrectionNotNeeded(
            "This register was never submitted. Take it on the register instead.",
        )
    if window_is_open(ctx):
        raise CorrectionNotNeeded()


@transaction.atomic
def request_correction(
    ctx: RegisterContext, user, student_id, *, to_mark, reason_code_id,
    note="", arrived_at=None,
) -> CorrectionRequest:
    """File a request to change one student's mark in a closed, submitted register.

    Refused while the day is still open, for a class the caller may not ask
    about, for the mark the student already has, for a mark that already has a
    request waiting, for a reason that is missing or retired, for a reason that
    asks for a note when there is none, and for Late without an arrival time.
    """
    _assert_correction_is_the_way(ctx)
    assert_may_request_correction(user, ctx.tenant, ctx.school_class)
    if to_mark not in Mark.values:
        raise ValidationError({"to_mark": "Choose Present, Late, Absent or Excused."})

    if reason_code_id in (None, ""):
        raise ReasonCodeRequired("Choose why the correction is needed.", field="reason_code")
    ensure_default_reason_codes(ctx.tenant)
    code = live_reason_code(ctx.tenant, reason_code_id)
    note = (note or "").strip()
    if code.requires_note and not note:
        raise NoteRequired(f"{code.name} needs a note saying what happened.", field="note")
    arrived = None
    if to_mark == Mark.LATE:
        if arrived_at is None:
            raise ArrivalTimeRequired(field="arrived_at")
        arrived = arrived_at.replace(second=0, microsecond=0)

    mark = (
        StudentMark.objects.select_for_update(of=("self",))
        .filter(register=ctx.register, student_id=student_id)
        .select_related("student").first()
    )
    if mark is None:
        raise NotFound("That student has no mark on this register.")
    if mark.mark == to_mark:
        raise NoChange(
            f"{mark.student.full_name} is already marked {Mark(to_mark).label}. "
            f"Choose a different mark.",
            field="to_mark",
        )
    if CorrectionRequest.objects.filter(mark=mark, status=CorrectionStatus.PENDING).exists():
        raise CorrectionAlreadyPending()

    correction = CorrectionRequest.objects.create(
        tenant=ctx.tenant, mark=mark, from_mark=mark.mark, to_mark=to_mark,
        arrived_at=arrived, reason_code=code, note=note, requested_by=user,
    )
    audit.record(
        tenant=ctx.tenant, user=user, branch=ctx.branch,
        entity_type="CorrectionRequest", entity_id=correction.pk,
        entity_label=audit.register_label(ctx.school_class, ctx.date, ctx.tenant, ctx.branch),
        action_type=AuditActionType.CREATE,
        summary=(
            f"A correction was asked for: {mark.student.full_name} from "
            f"{_label(mark.mark)} to {Mark(to_mark).label} in "
            f"{ctx.school_class.name}'s register."
        ),
        student_id=mark.student_id, from_mark=mark.mark, to_mark=to_mark,
        reason=code.name,
    )
    return correction


def corrections_visible_to(user, tenant, *, only_own: bool):
    """The corrections the caller may see, newest request first.

    A correction belongs to its register's branch, and a register is a
    transaction: a branch-bound caller sees their own branches' corrections and
    no others. *only_own* narrows a requester without the approve key to what
    they asked for themselves.
    """
    queryset = (
        CorrectionRequest.objects.filter(tenant=tenant)
        .filter(transaction_branch_q_for_user(user, "mark__register__"))
        .select_related(*_ROW_RELATIONS)
    )
    if only_own:
        queryset = queryset.filter(requested_by=user)
    return queryset.order_by("-requested_at", "-id")


def pending_count(user, tenant, *, branch=None) -> int:
    """How many corrections wait for a decision, in the caller's branches."""
    queryset = CorrectionRequest.objects.filter(
        tenant=tenant, status=CorrectionStatus.PENDING,
    ).filter(transaction_branch_q_for_user(user, "mark__register__"))
    if branch is not None:
        queryset = queryset.filter(mark__register__branch=branch)
    return queryset.count()


def correction_row(correction: CorrectionRequest, *, several_branches: bool, viewer) -> dict:
    """One correction as a list row reads it, before Field Access is applied."""
    mark = correction.mark
    register = mark.register
    return {
        "id": correction.pk,
        "student": {
            "id": mark.student_id,
            "name": mark.student.full_name,
            "student_number": mark.student.student_number,
        },
        "class_id": register.school_class_id,
        "class_name": register.school_class.name,
        "branch": (
            {"id": register.branch_id, "name": register.branch.name}
            if several_branches else None
        ),
        "date": register.date,
        "from_mark": correction.from_mark,
        "to_mark": correction.to_mark,
        "arrived_at": correction.arrived_at,
        "reason_code": correction.reason_code_id,
        "reason_label": correction.reason_code.name if correction.reason_code_id else None,
        "note": correction.note,
        "requested_by_name": correction.requested_by.full_name if correction.requested_by_id else "",
        "requested_at": correction.requested_at,
        "status": correction.status,
        "decided_by_name": correction.decided_by.full_name if correction.decided_by_id else "",
        "decided_at": correction.decided_at,
        "decision_note": correction.decision_note,
        "is_mine": correction.requested_by_id == getattr(viewer, "pk", None),
    }


@transaction.atomic
def decide(
    tenant, user, correction_id, *, approve: bool, decision_note="",
) -> CorrectionRequest:
    """Approve or reject one waiting correction.

    Raises 404 for a correction that is not the caller's to see, whether it is
    another school's or another branch's. A caller may not decide a correction
    they asked for, and a correction already decided is refused.
    """
    correction = (
        CorrectionRequest.objects.select_for_update(of=("self",))
        .filter(tenant=tenant, pk=correction_id)
        .filter(transaction_branch_q_for_user(user, "mark__register__"))
        .select_related(*_ROW_RELATIONS, "mark__register__session")
        .first()
    )
    if correction is None:
        raise NotFound("No such correction at this school.")
    if correction.requested_by_id == user.pk:
        raise SelfDecision()
    if correction.status != CorrectionStatus.PENDING:
        raise AlreadyDecided(
            f"This correction was already "
            f"{CorrectionStatus(correction.status).label.lower()}.",
        )

    now = timezone.now()
    register = correction.mark.register
    label = audit.register_label(register.school_class, register.date, tenant, register.branch)
    student_name = correction.mark.student.full_name
    previous = correction.from_mark

    if approve:
        mark = (
            StudentMark.objects.select_for_update(of=("self",))
            .select_related("student", "register", "register__school_class",
                            "register__branch", "reason_code")
            .get(pk=correction.mark_id)
        )
        if mark.mark != correction.from_mark:
            raise CorrectionStale()
        _apply(mark, correction, user, now)
        MarkChange.objects.create(
            tenant=tenant, mark=mark, from_mark=previous, to_mark=mark.mark,
            changed_by=user, changed_at=now, via=ChangeVia.CORRECTION, note=correction.note,
        )
        correction.status = CorrectionStatus.APPROVED
        if previous == Mark.ABSENT and mark.mark != Mark.ABSENT:
            transaction.on_commit(partial(notices.send_correction_notice, mark, mark.mark))
        summary = (
            f"The correction for {student_name} from {_label(previous)} to "
            f"{Mark(correction.to_mark).label} in {register.school_class.name}'s "
            f"register was approved."
        )
    else:
        correction.status = CorrectionStatus.REJECTED
        summary = (
            f"The correction for {student_name} from {_label(previous)} to "
            f"{Mark(correction.to_mark).label} in {register.school_class.name}'s "
            f"register was rejected."
        )

    correction.decided_by = user
    correction.decided_at = now
    correction.decision_note = (decision_note or "").strip()
    correction.save(update_fields=[
        "status", "decided_by", "decided_at", "decision_note", "updated_at",
    ])
    audit.record(
        tenant=tenant, user=user, branch=register.branch,
        entity_type="CorrectionRequest", entity_id=correction.pk, entity_label=label,
        summary=summary, student_id=correction.mark.student_id,
        from_mark=previous, to_mark=correction.to_mark, decision=correction.status,
    )
    return correction


def _apply(mark: StudentMark, correction: CorrectionRequest, user, now: dt.datetime) -> None:
    """Move *mark* to what the correction asked for, with the details its mark carries.

    A Late mark carries the arrival time and an Excused mark the reason, and
    neither is kept on any other. The note on the row is replaced only when the
    correction brought one. A notice already sent stays recorded as sent, so
    the row still says the guardian was told.
    """
    mark.mark = correction.to_mark
    mark.arrived_at = correction.arrived_at if correction.to_mark == Mark.LATE else None
    mark.reason_code = correction.reason_code if correction.to_mark == Mark.EXCUSED else None
    if correction.note:
        mark.note = correction.note
    if mark.mark != Mark.ABSENT and mark.notice_state != NoticeState.SENT:
        mark.notice_state = NoticeState.NONE
    mark.save()
