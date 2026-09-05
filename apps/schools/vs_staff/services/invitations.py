"""Withdrawing an invitation that was never used.

A school that invited the wrong person needs the link to stop working, not
merely to be marked stale: the recipient is holding an email, and until the
token is consumed and the activation key rotated, clicking it still sets a
password on a real account at a real school.

**The staff record survives.** The employment status moves to TERMINATED with an
event recording the revocation, because a school that invited the wrong address
has a record of having done so, and deleting it is how the same mistake gets
made twice.

**Nothing is emailed.** There is no notification event for a withdrawn
invitation, and inventing one would tell somebody they had been un-hired by a
school they had not yet joined.

FRD M12 v2.1, FR-020.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..constants import EmploymentStatus
from ..exceptions import InvitationAlreadyAccepted, ReasonRequired
from . import audit


@transaction.atomic
def revoke(staff, *, reason, actor, request=None):
    """Kill the link, close the account, keep the record.

    Refused for any account past PENDING, and worded as the invitation having
    been accepted rather than as a missing row: the row is there and the caller
    is entitled to know why the action does not apply. Closing an account that
    has been used is a suspension, which is a different key and a different
    screen.
    """
    from vs_user.models import User, UserInvitation
    from vs_user.services.user import UserStatusService

    from ..models import StaffEmploymentEvent

    if not (reason or "").strip():
        raise ReasonRequired(
            "Say why this invitation is being withdrawn.", field="reason",
        )
    if staff.user.status != User.Status.PENDING:
        raise InvitationAlreadyAccepted(
            "This invitation has already been accepted, so there is nothing to "
            "revoke. Suspend the account instead.",
            account_status=staff.user.status,
        )

    # Consume the invitation, which is what actually kills the link: activation
    # refuses a used row before it looks at anything else, so the email in the
    # recipient's inbox stops working rather than merely reading as stale.
    invitation = UserInvitation.objects.filter(user=staff.user).first()
    if invitation is not None and not invitation.is_used:
        invitation.consume()

    from_status = staff.employment_status
    staff.employment_status = EmploymentStatus.TERMINATED
    staff.exit_date = timezone.localdate()
    staff.save(update_fields=["employment_status", "exit_date", "updated_at"])

    StaffEmploymentEvent.objects.create(
        tenant=staff.tenant, staff=staff, from_status=from_status,
        to_status=EmploymentStatus.TERMINATED, reason=reason,
        effective_date=timezone.localdate(),
        last_working_day=timezone.localdate(),
        note="Invitation withdrawn before it was accepted.", changed_by=actor,
    )
    UserStatusService.deactivate(staff.user, actor, request=request)
    audit.emit_invitation_revoked(staff, reason, actor=actor)
    return staff
