"""Scheduled RFQ reminder and expiry housekeeping."""
from celery import shared_task
from django.utils import timezone

from .constants import RfqInvitationStatus, RfqStatus
from .models import RfqInvitation
from . import vendor_portal


@shared_task(name="vs_procurement.dispatch_rfq_reminders")
def dispatch_rfq_reminders() -> dict:
    """Send idempotent 72-hour and 24-hour reminders, then expire open links."""
    now = timezone.now()
    sent = 0
    expired = 0
    invitations = RfqInvitation.objects.filter(
        rfq__rfq_status=RfqStatus.ISSUED,
    ).exclude(status__in=[
        RfqInvitationStatus.SUBMITTED,
        RfqInvitationStatus.DECLINED,
        RfqInvitationStatus.REVOKED,
    ]).select_related(
        "rfq__entity__tenant", "vendor",
    ).prefetch_related("recipients")
    for invitation in invitations.iterator(chunk_size=200):
        deadline = invitation.deadline
        if deadline is None:
            continue
        remaining = deadline - now
        if remaining.total_seconds() < 0:
            invitation.status = RfqInvitationStatus.EXPIRED
            invitation.save(update_fields=["status", "updated_at"])
            expired += 1
            continue
        stage = 2 if remaining.total_seconds() <= 24 * 3600 else 1 if remaining.total_seconds() <= 72 * 3600 else 0
        if stage == 0 or invitation.reminder_stage >= stage:
            continue
        raw = vendor_portal.make_invitation_token(invitation)
        delivered = False
        for recipient in invitation.recipients.all():
            delivered = vendor_portal._safe_notify(
                event_key="procurement.rfq_reminder",
                context=vendor_portal._recipient_context(invitation, recipient, raw),
                invitation=invitation,
                recipients=[recipient],
            ) or delivered
        if delivered:
            invitation.reminder_stage = stage
            invitation.last_reminder_at = now
            invitation.save(update_fields=["reminder_stage", "last_reminder_at", "updated_at"])
            sent += 1
    return {"reminded_invitations": sent, "expired_invitations": expired}
