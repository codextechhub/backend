# =============================================================================
# vs_notifications / tasks.py
#
# Celery tasks for vs_notifications.
#
# Tasks:
#   deliver_email_notification  — dispatches a single email Notification record
# =============================================================================

import logging

from celery import shared_task
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .constants import EMAIL_MAX_RETRIES, EMAIL_RETRY_BACKOFF_SEC

logger = logging.getLogger("vs_notifications.tasks")


@shared_task(bind=True, name="vs_notifications.deliver_email_notification")
def deliver_email_notification(self, notification_id: str):
    """
    Dispatch a single email Notification record to the email provider.

    Retrieves the Notification, sends via Django's EMAIL_BACKEND, and
    transitions status to SENT or FAILED.

    Retry behaviour:
        - Max retries and backoff seconds come from EMAIL_MAX_RETRIES and
          EMAIL_RETRY_BACKOFF_SEC in constants.py.
        - Retries use a fixed countdown. On final failure the record is marked FAILED.

    Idempotency guard:
        - If the notification is already SENT when the task runs (e.g. after
          a Celery broker restart), the task exits immediately without
          re-sending.

    Args:
        notification_id:  UUID string of the Notification record.
    """
    # Late import to avoid circular dependency at module load time
    from .models import Notification, NotificationStatus

    # ── Fetch record ──────────────────────────────────────────────────────
    try:
        notif = Notification.objects.select_for_update().get(id=notification_id)
    except Notification.DoesNotExist:
        # Record deleted — nothing to do
        logger.warning(
            "deliver_email_notification: Notification %s not found. Skipping.",
            notification_id,
        )
        return

    # ── Idempotency guard ─────────────────────────────────────────────────
    if notif.status == NotificationStatus.SENT:
        logger.debug(
            "deliver_email_notification: Notification %s already SENT. Skipping.",
            notification_id,
        )
        return

    # ── Resolve email address ─────────────────────────────────────────────
    email_addr = notif.effective_email
    if not email_addr:
        # Should not reach here — dispatch.py catches this pre-flight.
        # Guard defensively in case the record was created by another path.
        notif.status = NotificationStatus.FAILED
        notif.failure_reason = "NO_EMAIL_ADDRESS"
        notif.save(update_fields=["status", "failure_reason"])
        logger.error(
            "deliver_email_notification: Notification %s has no email address. Marked FAILED.",
            notification_id,
        )
        return

    # ── Attempt delivery ──────────────────────────────────────────────────
    try:
        with transaction.atomic():
            send_mail(
                subject=notif.subject,
                message=notif.body,
                from_email=None,  # Uses DEFAULT_FROM_EMAIL from Django settings
                recipient_list=[email_addr],
                fail_silently=False,
            )

        notif.status = NotificationStatus.SENT
        notif.dispatched_at = timezone.now()
        notif.retry_count += 1
        notif.failure_reason = ""
        notif.save(update_fields=["status", "dispatched_at", "retry_count", "failure_reason"])

        logger.info(
            "deliver_email_notification: Notification %s SENT to %s (attempt %d).",
            notification_id,
            email_addr,
            notif.retry_count,
        )

    except Exception as exc:
        notif.retry_count += 1
        notif.failure_reason = str(exc)
        notif.save(update_fields=["retry_count", "failure_reason"])

        logger.warning(
            "deliver_email_notification: Notification %s failed on attempt %d: %s",
            notification_id,
            notif.retry_count,
            exc,
        )

        if self.request.retries < EMAIL_MAX_RETRIES:
            raise self.retry(exc=exc, countdown=EMAIL_RETRY_BACKOFF_SEC)
        else:
            # Final failure — mark and exit
            notif.status = NotificationStatus.FAILED
            notif.save(update_fields=["status"])
            logger.error(
                "deliver_email_notification: Notification %s FAILED after %d attempts. "
                "Last error: %s",
                notification_id,
                notif.retry_count,
                exc,
            )
