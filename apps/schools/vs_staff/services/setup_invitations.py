"""Staff imported while a school is set up, and invited when it goes live.

A school loads its staff list during onboarding, because the data step needs
it, and nobody on that list should be emailed a sign-in link to a school that
is not open yet. So a staff import at an onboarding school writes the account,
the starting role and the record, and stops short of the invitation: the
account stays ``PENDING_APPROVAL`` and the record reads ``AWAITING_GO_LIVE``,
"Invited at go-live".

When the school goes live (``vs_onboarding.services.go_live.approve_go_live``)
:func:`release_all` sends every held invitation, once:

* **one person at a time, each in its own transaction**, so one account that
  cannot be invited is written down and skipped, and neither blocks the others
  nor undoes the go-live, which has already committed;
* **idempotent**: a released record reads Invited, so running it again finds
  nobody. A person left held by a release that did not finish is invited by
  the ordinary Resend once the school is live (:func:`release`);
* **no hire approval**: a school that approves each hire approves its setup
  list by going live, so these invitations do not wait for the ladder.

A person imported with Send Invitation set to No gets their invitation created
and left unsent, as they would have at a live school. Revoking a held
invitation closes the record the way a refused hire is closed.
"""
from __future__ import annotations

import logging

from django.db import transaction

from vs_config.clock import tenant_today

from ..constants import EmploymentStatus
from . import audit

logger = logging.getLogger("vs_staff")

#: Why a resend is refused while the school is still being set up.
HELD_UNTIL_GO_LIVE = (
    "Invitations for staff imported during setup go out when the school goes live."
)


@transaction.atomic
def release(profile, *, actor=None):
    """Send one held invitation and move the record to Invited.

    Does nothing for a record that is not held, so a repeated release invites
    once. ``invite_on_approval`` decides whether the email goes out.
    """
    from vs_user.models import User
    from vs_user.services.user import UserCreationService

    from ..models import StaffEmploymentEvent, StaffProfile

    locked = (
        StaffProfile.all_objects.select_for_update()
        .select_related("user", "tenant").filter(pk=profile.pk).first()
    )
    if locked is None or locked.employment_status != EmploymentStatus.AWAITING_GO_LIVE:
        return None
    user = locked.user
    if user.status == User.Status.PENDING_APPROVAL:
        UserCreationService.finalize_invitation(
            user=user, requested_by=actor or locked.created_by,
            send_email=locked.invite_on_approval,
        )
    locked.employment_status = EmploymentStatus.INVITED
    locked.save(update_fields=["employment_status", "updated_at"])
    event = StaffEmploymentEvent.objects.create(
        tenant=locked.tenant, staff=locked,
        from_status=EmploymentStatus.AWAITING_GO_LIVE,
        to_status=EmploymentStatus.INVITED,
        effective_date=tenant_today(locked.tenant),
        reason="Invited when the school went live", changed_by=actor,
    )
    audit.emit_employment_status_changed(locked, event, actor=actor)
    return event


def release_all(tenant, *, actor=None) -> dict:
    """Send every invitation held for go-live at *tenant*.

    Returns ``{"sent": [...], "failed": [...]}`` of staff ids. A failure is
    logged and audited against the person, with the error, and the rest carry
    on.
    """
    from ..models import StaffProfile

    sent, failed = [], []
    held = StaffProfile.all_objects.filter(
        tenant=tenant, employment_status=EmploymentStatus.AWAITING_GO_LIVE,
    ).select_related("user").order_by("pk")
    for profile in held:
        try:
            if release(profile, actor=actor) is not None:
                sent.append(profile.pk)
        except Exception as exc:  # noqa: BLE001 - one person must not stop the rest
            failed.append(profile.pk)
            logger.exception(
                "Could not send the go-live invitation for staff %s at %s.",
                profile.pk, getattr(tenant, "slug", tenant),
            )
            audit.emit_setup_invitation_failed(profile, str(exc), actor=actor)
    return {"sent": sent, "failed": failed}
