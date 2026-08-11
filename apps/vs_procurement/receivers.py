"""Keep RFQ invitation delivery state aligned with notification outcomes."""
import logging

from django.dispatch import receiver

from vs_notifications.signals import notification_failed, notification_sent

from .constants import RfqInvitationStatus

logger = logging.getLogger(__name__)


def _update(notification, success: bool) -> None:
    try:
        if notification.event_type.key != "procurement.rfq_invitation":
            return
        invitation_id = (notification.metadata or {}).get("invitation_id")
        if not invitation_id:
            return
        from .models import RfqInvitation

        invitation = RfqInvitation.objects.filter(pk=invitation_id).first()
        if invitation is None:
            return
        if success:
            if invitation.status == RfqInvitationStatus.PENDING:
                invitation.status = RfqInvitationStatus.SENT
        elif invitation.status not in (
            RfqInvitationStatus.OPENED, RfqInvitationStatus.DRAFTING,
            RfqInvitationStatus.SUBMITTED, RfqInvitationStatus.DECLINED,
        ):
            invitation.status = RfqInvitationStatus.BOUNCED
        invitation.save(update_fields=["status", "updated_at"])
    except Exception:
        logger.exception("Could not update RFQ invitation delivery state")


@receiver(notification_sent, dispatch_uid="vs_procurement.rfq_invitation_sent")
def on_invitation_sent(sender, notification, **kwargs):
    _update(notification, True)


@receiver(notification_failed, dispatch_uid="vs_procurement.rfq_invitation_failed")
def on_invitation_failed(sender, notification, **kwargs):
    _update(notification, False)


@receiver(notification_sent, dispatch_uid="vs_procurement.purchase_order_email_sent")
def on_purchase_order_email_sent(sender, notification, **kwargs):
    """Advance a purchase-order delivery after each recipient email succeeds."""
    if (notification.metadata or {}).get("po_delivery_id"):
        from .po_email import update_from_notification
        update_from_notification(notification, success=True)


@receiver(notification_failed, dispatch_uid="vs_procurement.purchase_order_email_failed")
def on_purchase_order_email_failed(sender, notification, **kwargs):
    """Make final purchase-order delivery failures visible and retryable."""
    if (notification.metadata or {}).get("po_delivery_id"):
        from .po_email import update_from_notification
        update_from_notification(notification, success=False)
