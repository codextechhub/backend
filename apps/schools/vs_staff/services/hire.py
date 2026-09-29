"""Approving a new member of staff before their invitation is sent.

A school may decide that nobody is invited until a second person has agreed to
the hire (``staff.hire.requires_approval``). Where it has, adding somebody
creates the account and the staff record exactly as an ordinary add does, the
starting role included, and stops short of the invitation: the account stays
``PENDING_APPROVAL``, the record reads Awaiting approval, and the record is
submitted to the school's New staff approval ladder (``approvals.py``).

The ladder's decision is carried out here, from the workflow handler:

* **approved**: the invitation is created and, unless the person was imported
  with Send Invitation set to No, emailed; the record moves to Invited and the
  ordinary path resumes;
* **rejected, withdrawn or cancelled**: the hire is closed the way the
  platform closes a refused hire of its own. The record moves to Terminated
  with the reason, a post reserved for the person is released, every role
  grant written at creation is revoked, and the account is REJECTED, so it can
  never be signed into.

Nothing is emailed on a closed hire: the person was never told they were being
hired. A school still onboarding never waits for approval, because the
administrators it is inviting are the people who would approve.
"""
from __future__ import annotations

from django.db import transaction

from vs_config.clock import tenant_today

from ..constants import EmploymentStatus
from . import audit


def needs_approval(tenant) -> bool:
    """Whether a person added at *tenant* now waits for approval before being invited."""
    from vs_tenants.models import Tenant

    from .rules import hire_requires_approval

    if getattr(tenant, "status", None) == Tenant.Status.PENDING:
        return False
    return hire_requires_approval(tenant)


@transaction.atomic
def submit(profile, *, actor):
    """Put a hire awaiting approval in front of the school's ladder.

    The ladder is published first where the school has none, so a hire is
    never refused for want of a template the setting implied.
    """
    from vs_workflow.services.submission import submit_for_approval

    from ..approvals import ensure_hire_approval_template

    ensure_hire_approval_template(profile.tenant, created_by=actor)
    return submit_for_approval(profile, actor)


def _event(profile, *, to_status, reason, note="", actor=None, last_working_day=None,
           from_status=EmploymentStatus.PENDING_APPROVAL):
    from ..models import StaffEmploymentEvent

    event = StaffEmploymentEvent.objects.create(
        tenant=profile.tenant, staff=profile,
        from_status=from_status, to_status=to_status,
        reason=reason, effective_date=tenant_today(profile.tenant),
        last_working_day=last_working_day, note=note, changed_by=actor,
    )
    audit.emit_employment_status_changed(profile, event, actor=actor)
    return event


@transaction.atomic
def approve(profile, *, requested_by):
    """Send the invitation the approval was holding back, and move to Invited.

    Does nothing for a record no longer awaiting approval, so a callback that
    arrives twice invites once.
    """
    from vs_user.models import User
    from vs_user.services.user import UserCreationService

    if profile.employment_status != EmploymentStatus.PENDING_APPROVAL:
        return None
    user = profile.user
    if user.status == User.Status.PENDING_APPROVAL:
        UserCreationService.finalize_invitation(
            user=user, requested_by=requested_by,
            send_email=profile.invite_on_approval,
        )
    profile.employment_status = EmploymentStatus.INVITED
    profile.save(update_fields=["employment_status", "updated_at"])
    return _event(
        profile, to_status=EmploymentStatus.INVITED, reason="Hire approved",
    )


@transaction.atomic
def close(profile, *, reason, actor=None):
    """Close a hire that was not approved: record, post, grants and account.

    Does nothing for a record no longer awaiting approval: an invited person is
    withdrawn by revoking their invitation, which has rules of its own.
    """
    if profile.employment_status != EmploymentStatus.PENDING_APPROVAL:
        return None
    return close_unsent(
        profile, reason=reason, actor=actor,
        note="The hire was not approved, so no invitation was sent.",
    )


#: The statuses of a record whose invitation has not been sent yet.
UNSENT_STATUSES = frozenset({
    EmploymentStatus.PENDING_APPROVAL, EmploymentStatus.AWAITING_GO_LIVE,
})


@transaction.atomic
def close_unsent(profile, *, reason, note, actor=None):
    """Close a record whose invitation was never sent: record, post, grants, account.

    The way the platform closes a refused hire of its own. The record moves to
    Terminated with the reason and *note*, a post reserved for the person is
    released, every active role grant is revoked, and the account, which was
    never offered to anybody, is REJECTED so it can never be signed into.
    Nothing is emailed. Does nothing for a record that is not unsent.
    """
    from vs_rbac.models import TenantUserRoleAssignment
    from vs_user.models import User

    from .organogram import StaffOrganogramService

    from_status = profile.employment_status
    if from_status not in UNSENT_STATUSES:
        return None
    today = tenant_today(profile.tenant)
    profile.employment_status = EmploymentStatus.TERMINATED
    profile.exit_date = today
    profile.save(update_fields=["employment_status", "exit_date", "updated_at"])
    event = _event(
        profile, to_status=EmploymentStatus.TERMINATED, reason=reason,
        note=note, actor=actor, last_working_day=today, from_status=from_status,
    )
    StaffOrganogramService.close_for_exit(profile, today)

    for grant in TenantUserRoleAssignment.objects.filter(
        user=profile.user,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ):
        grant.revoke(by_user=actor, reason=reason)
        grant.save(update_fields=[
            "assignment_status", "revoked_at", "revoked_by", "reason_note",
            "updated_at",
        ])

    user = profile.user
    if user.status == User.Status.PENDING_APPROVAL:
        user.status = User.Status.REJECTED
        user.save(update_fields=["status", "is_active", "updated_at"])
    return event


def withdraw(profile, *, reason, actor):
    """Call off a hire awaiting approval, as the school's own act.

    Cancels the workflow instance, whose handler closes the hire through
    :func:`close` with this reason, so the ladder and the record cannot
    disagree about whether the hire is still wanted.
    """
    from vs_workflow.models import WorkflowInstance
    from vs_workflow.services import actions as workflow_actions

    instance = (
        WorkflowInstance.all_objects.filter(
            document_type=profile.workflow_document_type,
            document_object_id=str(profile.pk),
        )
        .order_by("-pk")
        .first()
    )
    if instance is not None and not instance.is_terminal:
        workflow_actions.cancel(instance.id, actor, reason)
    profile.refresh_from_db()
    if profile.employment_status == EmploymentStatus.PENDING_APPROVAL:
        close(profile, reason=reason, actor=actor)
    return profile
