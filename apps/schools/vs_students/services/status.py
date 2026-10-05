"""The status state machine.

**One function is the only thing in this module that writes Student.status.**
Every route that changes a status goes through :func:`transition`, so the
transition table, the reason rule, the log row and the audit event exist once
rather than eight times and drift nowhere.

FRD M11 v2.4 FR-011.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_audit.services import emit_audit_event

from ..constants import (
    ALLOWED_TRANSITIONS,
    LEAVES_THE_ROLL,
    ON_ROLL,
    REASON_OPTIONAL,
    EnrolmentOutcome,
    StudentStatus,
)
from ..exceptions import (
    DestinationRequired,
    InvalidStatusTransition,
    ReasonRequired,
    SuspensionEndsBeforeItStarts,
    TerminalStatus,
)
from ..models import StudentStatusLog

logger = logging.getLogger(__name__)

#: Which audit action each destination writes. A status change is the event a
#: school looks up by name later, so it gets its own verb rather than a generic
#: UPDATE that says only that something changed.
_AUDIT = {
    StudentStatus.ENROLLED: AuditActionType.STUDENT_ENROLLED,
    StudentStatus.ACTIVE: AuditActionType.STUDENT_REACTIVATED,
    StudentStatus.SUSPENDED: AuditActionType.STUDENT_SUSPENDED,
    StudentStatus.WITHDRAWN: AuditActionType.STUDENT_WITHDRAWN,
    StudentStatus.GRADUATED: AuditActionType.STUDENT_GRADUATED,
    StudentStatus.TRANSFERRED: AuditActionType.STUDENT_TRANSFERRED_OUT,
    StudentStatus.REJECTED: AuditActionType.STUDENT_REJECTED,
}

#: The audit actions a status move writes, for a reader of the trail that has
#: to know which rows can carry the reason behind one.
STATUS_AUDIT_ACTIONS = frozenset(_AUDIT.values())

#: Where the reason began in an audit summary that carries it. The summary a
#: status move or an admission stage move writes keeps the reason in
#: ``metadata``, but the trail is immutable and can hold summaries of either
#: that end ``" Reason: <words>"``, so a reader cuts them here
#: (``field_access.register_audit_words``).
SUMMARY_REASON_MARKER = " Reason: "

#: What each destination means for the child, said the way the screen says it.
#: The design prints these verbatim in the confirmation panel, so they are API
#: output and not documentation.
IMPACT = {
    StudentStatus.SUSPENDED: (
        "A suspended student keeps their class seat but is marked out of "
        "attendance."
    ),
    StudentStatus.WITHDRAWN: (
        "A withdrawn student leaves the roll and their class seat is released. "
        "Their record and history are kept."
    ),
    StudentStatus.TRANSFERRED: (
        "A transferred student leaves the roll. Their record and history stay "
        "for reference."
    ),
    StudentStatus.GRADUATED: (
        "A graduating student leaves the roll as an alumnus. Their full record "
        "is kept."
    ),
    StudentStatus.ACTIVE: (
        "The student returns to normal attendance from the effective date."
    ),
    StudentStatus.ENROLLED: (
        "The student is put back on the roll and will need a class assigned."
    ),
    StudentStatus.REJECTED: (
        "The application is closed. The record is kept but the applicant is "
        "not enrolled."
    ),
}


def allowed_from(status) -> list[str]:
    """The moves a school may make from *status*, in a stable order."""
    order = list(StudentStatus.values)
    return [s for s in order if s in ALLOWED_TRANSITIONS.get(status, frozenset())]


def assert_can_change(student):
    """Refuse to open the status form at all on a terminal status.

    The design shows a sentence rather than a form here, so this is the
    refusal that carries it.
    """
    if not ALLOWED_TRANSITIONS.get(student.status):
        raise TerminalStatus(
            f"{StudentStatus(student.status).label} is a final status. There "
            f"is nothing to move {student.first_name} to.",
            status=student.status,
        )


@transaction.atomic
def transition(
    student, to_status, *, actor, reason="", effective_date=None,
    destination_school="", return_date=None, send_reason=False, system=False,
):
    """Move *student* to *to_status*, or refuse and write nothing at all.

    A refused transition writes no log row and no audit event. That is asserted
    by a test rather than assumed, because a log row without a reason is worse
    than no log row: it looks like a decision somebody made.

    ``system=True`` is a batch, such as the promotion run or the automatic
    return from suspension, which supplies its own sentence and has no human
    actor to attribute the change to.

    ``return_date`` belongs to a suspension and is cleared on every other
    destination, in the same way ``destination_school`` is: a field that means
    nothing for the move being recorded is not kept on the row because a
    caller sent it. ``send_reason`` decides only what the family reads, never
    what the history holds: the reason is written to the record either way,
    and it reaches a guardian's notice only where the person suspending the
    pupil chose to send it.

    The audit event carries the reason in ``metadata["reason"]`` and never in
    its summary. The reason is a Field Access field (``status_reason``), and
    a summary is one sentence that every surface printing the trail shows
    whole, so no Read switch could take the words back out of it.
    """
    from_status = student.status
    if to_status == from_status:
        raise InvalidStatusTransition(
            f"{student.first_name} is already "
            f"{StudentStatus(to_status).label.lower()}.",
            **{"from": from_status, "to": to_status},
        )
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        raise InvalidStatusTransition(
            f"{student.first_name} is "
            f"{StudentStatus(from_status).label.lower()}, so they cannot be "
            f"moved to {StudentStatus(to_status).label.lower()}.",
            **{"from": from_status, "to": to_status},
        )

    reason = (reason or "").strip()
    if not reason and not system and to_status not in REASON_OPTIONAL:
        raise ReasonRequired()

    destination_school = (destination_school or "").strip()
    if to_status == StudentStatus.TRANSFERRED and not destination_school:
        raise DestinationRequired()
    if to_status != StudentStatus.TRANSFERRED:
        destination_school = ""

    effective_date = effective_date or branch_today(student.tenant, student.branch_id)

    if to_status != StudentStatus.SUSPENDED:
        return_date = None
    if return_date is not None and return_date <= effective_date:
        raise SuspensionEndsBeforeItStarts(
            effective_date=str(effective_date), return_date=str(return_date),
        )

    student.status = to_status
    student.save(update_fields=["status", "updated_at"])

    if to_status in LEAVES_THE_ROLL:
        _release_seat(student, to_status)
    _sync_billing(student, from_status, to_status, actor=None if system else actor)

    StudentStatusLog.objects.create(
        tenant=student.tenant, student=student,
        from_status=from_status, to_status=to_status,
        reason=reason, effective_date=effective_date,
        return_date=return_date,
        destination_school=destination_school,
        changed_by=None if system else actor,
    )

    summary = (
        f"{student.full_name} moved from {StudentStatus(from_status).label} to "
        f"{StudentStatus(to_status).label} on "
        f"{format_date(effective_date, student.tenant)}."
    )
    if destination_school:
        summary += f" Destination: {destination_school}."
    if return_date:
        summary += (
            f" Expected back on "
            f"{format_date(return_date, student.tenant)}."
        )

    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=_AUDIT.get(to_status, AuditActionType.UPDATE),
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=None if system else actor,
        summary=summary,
        metadata={
            "from": from_status, "to": to_status,
            "effective_date": str(effective_date),
            "return_date": str(return_date) if return_date else "",
            "destination_school": destination_school,
            "reason": reason,
        },
    )

    _notify_guardians(
        student, to_status, effective_date,
        return_date=return_date, reason=reason if send_reason else "",
    )
    return student


def _notify_guardians(student, to_status, effective_date, *, return_date, reason):
    """Tell the pupil's guardians about a suspension, and about nothing else.

    Only a move INTO SUSPENDED writes to a family, which a destination of
    SUSPENDED is enough to establish: a move to the status a pupil already
    holds is refused before anything is written. A suspension being lifted
    sends nothing, by hand or by the automatic return: there is no event for
    it, and a school lifting one has already spoken to the family it suspended
    the pupil from.

    *reason* arrives already emptied where the person suspending the pupil did
    not choose to send it, so this function never decides what a family reads.

    Who is written to, what the notice says and why a failure cannot block the
    suspension all live in :mod:`schools.vs_students.services.suspension_notice`.
    """
    from .suspension_notice import send_suspension_notice

    if to_status != StudentStatus.SUSPENDED:
        return
    send_suspension_notice(
        student, effective_date, return_date=return_date, reason=reason,
    )


def _sync_billing(student, from_status, to_status, *, actor):
    """Stop billing a child who leaves the roll, and bill them again when readmitted.

    A withdrawn, transferred or graduated child's finance account is deactivated
    through the FAL, so no fee run and no "bill all active" selection bills them
    again, while what they still owe stays owed and on the debtor list. A withdrawn
    child readmitted (WITHDRAWN to ENROLLED) is reactivated. A finance backend that
    does not answer does not block the status change; it is logged, because the
    account would otherwise go on being billed unnoticed.
    """
    from schools.core.fal import get_student_customer

    leaving = to_status in LEAVES_THE_ROLL
    returning = from_status in LEAVES_THE_ROLL and to_status in ON_ROLL
    if not (leaving or returning):
        return
    result = get_student_customer().set_customer_active(
        student.pk, active=returning,
        reason=f"Student moved from {from_status} to {to_status}.",
        actor_ref=getattr(actor, "pk", None),
    )
    if not result.is_available:
        logger.warning(
            "Finance account of student %s was not %s: %s", student.pk,
            "reactivated" if returning else "deactivated", result.reason,
        )


def _release_seat(student, to_status):
    """Leaving the roll releases the class seat; the history keeps the row.

    Suspension deliberately does not come through here: a suspended child keeps
    their seat, and that is the whole difference between suspending and
    withdrawing.
    """
    outcome = (
        EnrolmentOutcome.GRADUATED if to_status == StudentStatus.GRADUATED
        else EnrolmentOutcome.TRANSFERRED if to_status == StudentStatus.TRANSFERRED
        else EnrolmentOutcome.ENDED
    )
    student.enrolments.filter(is_active=True).update(
        is_active=False, ended_at=timezone.now(), outcome=outcome,
    )
