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

logger = logging.getLogger("vs_notifications.tasks")


@shared_task(bind=True, name="vs_notifications.deliver_email_notification")
def deliver_email_notification(self, notification_id: str):
    """
    Dispatch a single email Notification record to the email provider.

    Retrieves the Notification, sends via Django's EMAIL_BACKEND, and
    transitions status to SENT or FAILED.

    Retry behaviour:
        - Max retries and backoff seconds are read from vs_config at
          execution time so they can be adjusted without redeployment.
        - Retries use a fixed countdown (not exponential) as defined in FRD.
        - On final failure the record is marked FAILED and the task exits.

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

    # ── Read retry config ─────────────────────────────────────────────────
    max_retries, retry_backoff = _read_retry_config()

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

        if self.request.retries < max_retries:
            # Retry with fixed countdown
            raise self.retry(exc=exc, countdown=retry_backoff)
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


def _read_retry_config() -> tuple[int, int]:
    """
    Read max retries and backoff seconds from vs_config.
    Falls back to hardcoded defaults if vs_config is unavailable.
    """
    from .constants import NotificationConfigKey

    try:
        from vs_config.services import FlagService
        max_retries = FlagService.get_int(
            NotificationConfigKey.EMAIL_MAX_RETRIES,
            default=NotificationConfigKey.DEFAULTS[NotificationConfigKey.EMAIL_MAX_RETRIES],
        )
        retry_backoff = FlagService.get_int(
            NotificationConfigKey.EMAIL_RETRY_BACKOFF_SEC,
            default=NotificationConfigKey.DEFAULTS[NotificationConfigKey.EMAIL_RETRY_BACKOFF_SEC],
        )
    except Exception:
        max_retries = NotificationConfigKey.DEFAULTS[NotificationConfigKey.EMAIL_MAX_RETRIES]
        retry_backoff = NotificationConfigKey.DEFAULTS[NotificationConfigKey.EMAIL_RETRY_BACKOFF_SEC]

    return max_retries, retry_backoff
