# receivers.py
# Delivery-signal receivers that keep UserInvitation email tracking in sync
# with the notification engine.
#
# The invitation email is dispatched through vs_notifications; the engine fires
# notification_sent / notification_failed on terminal delivery. These receivers
# correlate the terminal record back to its UserInvitation via the
# activation_key carried in Notification.metadata and update the same tracking
# fields the old bypass task wrote (email_attempts / email_status / email_sent_at
# / email_last_error).
#
# Receivers must never raise — a tracking failure must not break dispatch or the
# delivery task. They filter on the user.invited event key so other events are
# ignored cheaply.

import logging

from django.dispatch import receiver

from vs_notifications.signals import notification_sent, notification_failed

logger = logging.getLogger('vs_user.receivers')

_INVITED_EVENT_KEY = "user.invited"


def _update_invitation(notification, *, success: bool):
    """Apply one delivery outcome to the correlated UserInvitation. Never raises."""
    try:
        if notification.event_type.key != _INVITED_EVENT_KEY:
            return

        activation_key = (notification.metadata or {}).get("activation_key")
        if not activation_key:
            return

        from .models import UserInvitation

        inv = UserInvitation.objects.get(user__activation_key=activation_key)
        inv.email_attempts += 1
        if success:
            inv.email_status     = UserInvitation.EmailStatus.SENT
            inv.email_sent_at    = notification.dispatched_at
            inv.email_last_error = ''
        else:
            inv.email_status     = UserInvitation.EmailStatus.FAILED
            # The engine stores the raw exception string (or a sentinel such as
            # NO_EMAIL_ADDRESS for pre-flight failures) on failure_reason.
            inv.email_last_error = notification.failure_reason
        inv.save(update_fields=[
            'email_attempts', 'email_status', 'email_sent_at',
            'email_last_error', 'updated_at',
        ])
    except Exception:
        logger.exception(
            'Failed to update invitation email status from notification %s',
            getattr(notification, 'id', None),
        )


@receiver(notification_sent, dispatch_uid="vs_user.invitation_sent")
def on_invitation_sent(sender, notification, **kwargs):
    _update_invitation(notification, success=True)


@receiver(notification_failed, dispatch_uid="vs_user.invitation_failed")
def on_invitation_failed(sender, notification, **kwargs):
    _update_invitation(notification, success=False)
