"""The status state machine.

**One function is the only thing in this module that writes Student.status.**
Every route that changes a status goes through :func:`transition`, so the
transition table, the reason rule, the log row and the audit event exist once
rather than eight times and drift nowhere.

FRD M11 v2.4 FR-011.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    ALLOWED_TRANSITIONS,
    LEAVES_THE_ROLL,
    EnrolmentOutcome,
    StudentStatus,
)
from ..exceptions import (
    DestinationRequired,
    InvalidStatusTransition,
    ReasonRequired,
    TerminalStatus,
)
from ..models import StudentStatusLog

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
    destination_school="", system=False,
):
    """Move *student* to *to_status*, or refuse and write nothing at all.

    A refused transition writes no log row and no audit event. That is asserted
    by a test rather than assumed, because a log row without a reason is worse
    than no log row: it looks like a decision somebody made.

    ``system=True`` is the promotion batch, which supplies its own sentence and
    has no human actor to attribute the change to.
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
    if not reason and not system:
        raise ReasonRequired()

    destination_school = (destination_school or "").strip()
    if to_status == StudentStatus.TRANSFERRED and not destination_school:
        raise DestinationRequired()
    if to_status != StudentStatus.TRANSFERRED:
        destination_school = ""

    effective_date = effective_date or timezone.localdate()

    student.status = to_status
    student.save(update_fields=["status", "updated_at"])

    if to_status in LEAVES_THE_ROLL:
        _release_seat(student, to_status)

    StudentStatusLog.objects.create(
        tenant=student.tenant, student=student,
        from_status=from_status, to_status=to_status,
        reason=reason, effective_date=effective_date,
        destination_school=destination_school,
        changed_by=None if system else actor,
    )

    summary = (
        f"{student.full_name} moved from {StudentStatus(from_status).label} to "
        f"{StudentStatus(to_status).label} on {effective_date}."
    )
    if destination_school:
        summary += f" Destination: {destination_school}."
    if reason:
        summary += f" Reason: {reason}"

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
            "destination_school": destination_school,
        },
    )
    return student


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
