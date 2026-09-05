"""Every audit event this module emits, in one place.

``emit_audit_event`` never raises and silently drops an action type that is not
registered, so a module that emits a value nobody added to
:class:`vs_audit.models.AuditActionType` gets an empty trail and no complaint.
Keeping the calls together is what makes that easy to check: the vocabulary this
file uses is exactly the STAFF block in that class.

Actors are passed as User objects rather than ids, and never as email addresses.
A history is the most widely read part of a profile, and the platform's rule
that an actor is an id and a display name applies here more than anywhere.
"""
from __future__ import annotations

from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
from vs_audit.services import emit_audit_event

MODULE = AuditModuleKey.STAFF


def _name(staff) -> str:
    user = getattr(staff, "user", None)
    if user is None:
        return ""
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip()


def emit_profile_created(staff, actor=None):
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.CREATE,
        entity_type="StaffProfile", entity_id=str(staff.pk),
        entity_label=_name(staff), actor_user=actor, tenant=staff.tenant,
        summary=f"Added {_name(staff)} to the staff list.",
    )


def emit_employment_status_changed(staff, event, actor=None):
    emit_audit_event(
        module_key=MODULE,
        action_type=AuditActionType.STAFF_EMPLOYMENT_STATUS_CHANGED,
        entity_type="StaffProfile", entity_id=str(staff.pk),
        entity_label=_name(staff), actor_user=actor, tenant=staff.tenant,
        severity=AuditSeverity.WARNING,
        summary=(
            f"Employment status moved from {event.from_status or 'new'} "
            f"to {event.to_status}."
        ),
        metadata={
            "from_status": event.from_status,
            "to_status": event.to_status,
            "reason": event.reason,
            "effective_date": str(event.effective_date),
            "last_working_day": (
                str(event.last_working_day) if event.last_working_day else None
            ),
        },
    )


def emit_posting_changed(staff, previous, actor=None):
    """One event per person, never one per bulk call.

    A bulk action that writes a single event is a bulk action nobody can audit
    per person, and "who moved Mrs. Adeyemi to Ikeja" is exactly the question a
    trail is read for.
    """
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_POSTING_CHANGED,
        entity_type="StaffProfile", entity_id=str(staff.pk),
        entity_label=_name(staff), actor_user=actor, tenant=staff.tenant,
        summary=f"Posting changed for {_name(staff)}.",
        metadata={
            "from_branch_id": previous,
            "to_branch_id": staff.branch_id,
            # Said in the metadata as well as in the response, because the
            # question "did this move their access too" is asked of the trail
            # long after the confirmation dialog is gone.
            "role_grants_touched": False,
        },
    )


def emit_teaching_assigned(assignment, actor=None):
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_TEACHING_ASSIGNED,
        entity_type="TeachingAssignment", entity_id=str(assignment.pk),
        entity_label=_name(assignment.staff), actor_user=actor,
        tenant=assignment.tenant,
        summary=(
            f"Assigned as {assignment.get_part_display().lower()} for "
            f"{assignment.subject.name} in {assignment.school_class.name}."
        ),
        metadata={
            "staff_id": assignment.staff_id,
            "class_id": assignment.school_class_id,
            "subject_id": assignment.subject_id,
            "session_id": assignment.session_id,
            "part": assignment.part,
        },
    )


def emit_teaching_unassigned(assignment, actor=None):
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_TEACHING_UNASSIGNED,
        entity_type="TeachingAssignment", entity_id=str(assignment.pk),
        entity_label=_name(assignment.staff), actor_user=actor,
        tenant=assignment.tenant,
        severity=AuditSeverity.WARNING,
        summary=(
            f"Removed from {assignment.subject.name} in "
            f"{assignment.school_class.name}."
        ),
        metadata={
            "staff_id": assignment.staff_id,
            "class_id": assignment.school_class_id,
            "subject_id": assignment.subject_id,
            "session_id": assignment.session_id,
            "part": assignment.part,
        },
    )


def emit_class_teacher_set(school_class, previous_staff_id, actor=None, tenant=None):
    """Both the old designation and the new one, because replacing is silent.

    Setting a class teacher where one already sits removes somebody from a job
    they held, and a trail that records only the arrival cannot answer why the
    previous holder stopped appearing.
    """
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_TEACHING_ASSIGNED,
        entity_type="SchoolClass", entity_id=str(school_class.pk),
        entity_label=school_class.name, actor_user=actor,
        tenant=tenant or school_class.tenant,
        summary=f"Class teacher set for {school_class.name}.",
        metadata={
            "from_staff_id": previous_staff_id,
            "to_staff_id": school_class.class_teacher_id,
            "class_teacher": True,
        },
    )


def emit_leave_recorded(leave, actor=None):
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_LEAVE_RECORDED,
        entity_type="LeaveRequest", entity_id=str(leave.pk),
        entity_label=_name(leave.staff), actor_user=actor, tenant=leave.tenant,
        # SENSITIVE: sick leave says something about a person's health, and the
        # trail is read by people the leave list itself is closed to.
        severity=AuditSeverity.CRITICAL,
        summary=f"Leave filed: {leave.get_leave_type_display()}.",
        metadata={
            "staff_id": leave.staff_id,
            "leave_type": leave.leave_type,
            "start_date": str(leave.start_date),
            "end_date": str(leave.end_date),
            "days": leave.days,
            "status": leave.status,
        },
    )


def emit_leave_decided(leave, actor=None):
    """Separate from the filing, because they are different acts by different people.

    "Who asked" and "who allowed it" are two questions and a single event can
    only answer one of them.
    """
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_LEAVE_DECIDED,
        entity_type="LeaveRequest", entity_id=str(leave.pk),
        entity_label=_name(leave.staff), actor_user=actor, tenant=leave.tenant,
        severity=AuditSeverity.CRITICAL,
        summary=f"Leave {leave.status.lower()}.",
        metadata={
            "staff_id": leave.staff_id,
            "leave_type": leave.leave_type,
            "status": leave.status,
            "start_date": str(leave.start_date),
            "end_date": str(leave.end_date),
        },
    )


def emit_invitation_revoked(staff, reason, actor=None):
    emit_audit_event(
        module_key=MODULE, action_type=AuditActionType.STAFF_INVITATION_REVOKED,
        entity_type="StaffProfile", entity_id=str(staff.pk),
        entity_label=_name(staff), actor_user=actor, tenant=staff.tenant,
        severity=AuditSeverity.WARNING,
        summary=f"Invitation revoked before it was accepted.",
        metadata={"reason": reason},
    )
