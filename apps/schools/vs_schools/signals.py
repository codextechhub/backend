"""Receivers that keep this app's records in step with the engines beneath it.

Loaded by :meth:`VsSchoolsConfig.ready`, which is the only reason they are
connected at all - nothing imports this module for its contents.

Primary-admin invite status
    ``BranchPrimaryAdmin`` and ``SchoolPrimaryAdmin`` each carry an
    ``invite_status``, which is the console's answer to "did the head teacher
    ever get their invitation". Provisioning cannot write that answer: it asks
    for the email from inside the creation transaction, and the email is handed
    to a broker only once that transaction commits, by which time provisioning
    has returned. A status written at the earlier moment is a guess, and it was
    wrong in the one case that matters - Greenfield created while Redis is down
    reported its principal's invitation SENT, no task existed, no email arrived,
    and the school's first administrator could not activate it.

    ``vs_user`` announces the outcome instead, and this is where a school link
    learns of it. A retry that succeeds days later announces it again, so the
    console stops showing Failed for an invitation that has since gone out.

    Links are matched on the contact's address within the invited account's own
    tenant. The address is the only thing the two records share - an admin link
    points at a ``ContactInfo``, never at the ``User`` provisioning made from
    it - and scoping to the tenant is what keeps Bright Star's head teacher from
    settling Greenfield's link when both were entered under the same address.
"""
import logging

from django.dispatch import receiver
from django.utils import timezone

from vs_user.signals import invitation_dispatch_settled

logger = logging.getLogger("vs_schools.signals")


@receiver(invitation_dispatch_settled, dispatch_uid="vs_schools.primary_admin_invite")
def settle_primary_admin_invite(sender, invitation_id, user, accepted, **kwargs):
    """Record on the admin link whether an invitation email is actually on its way.

    Only links still waiting on an answer are touched. A link already SENT
    belongs to an invitation that was delivered; a later resend failing for the
    same person says nothing about the one they received and must not overwrite
    it with Failed.

    Never raises. The email is queued (or refused) by the time this runs and
    neither outcome can be undone by a status column failing to update.
    """
    from .models import BranchPrimaryAdmin, InviteStatus, SchoolPrimaryAdmin

    try:
        now = timezone.now()
        settled = InviteStatus.SENT if accepted else InviteStatus.FAILED
        unsettled = (InviteStatus.QUEUED, InviteStatus.FAILED)

        updated = 0
        for queryset in (
            BranchPrimaryAdmin.objects.filter(branch__tenant_id=user.tenant_id),
            SchoolPrimaryAdmin.objects.filter(school__tenant_id=user.tenant_id),
        ):
            updated += queryset.filter(
                contact__email__iexact=user.email,
                invite_status__in=unsettled,
            ).update(
                invite_status=settled,
                invite_sent_at=now if accepted else None,
                updated_at=now,
            )

        if updated and not accepted:
            logger.error(
                "settle_primary_admin_invite: %s admin link(s) marked FAILED for "
                "%s - the invitation was never handed to the broker.",
                updated, user.email,
            )
    except Exception:
        logger.exception(
            "settle_primary_admin_invite: could not settle admin links for "
            "invitation %s",
            invitation_id,
        )
