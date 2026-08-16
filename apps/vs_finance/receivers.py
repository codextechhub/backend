"""Keep finance document deliveries aligned with notification outcomes.

vs_notifications owns the send and its per-attempt retries, and reports the result
through :data:`~vs_notifications.signals.notification_sent` /
:data:`~vs_notifications.signals.notification_failed`. These receivers turn that into
the finance-side outcome a user reads on a document: sent, or failed with a reason
they can retry.

Only notifications carrying ``finance_delivery_id`` are ours, so the guard runs before
the import - every notification in the platform passes through here.
"""
import logging

from django.dispatch import receiver

from vs_notifications.signals import notification_failed, notification_sent

logger = logging.getLogger(__name__)


def _settle(notification, *, success: bool) -> None:
    if not (notification.metadata or {}).get("finance_delivery_id"):
        return
    try:
        from .document_email import update_from_notification

        update_from_notification(notification, success=success)
    except Exception:
        # A delivery stuck showing "pending" is a reporting problem; letting this
        # raise would fail the notification pipeline for every other app too.
        logger.exception("Could not settle finance document delivery state")


@receiver(notification_sent, dispatch_uid="vs_finance.document_email_sent")
def on_document_email_sent(sender, notification, **kwargs):
    """Mark a delivery sent once every recipient's email has succeeded."""
    _settle(notification, success=True)


@receiver(notification_failed, dispatch_uid="vs_finance.document_email_failed")
def on_document_email_failed(sender, notification, **kwargs):
    """Make a final delivery failure visible and retryable."""
    _settle(notification, success=False)
