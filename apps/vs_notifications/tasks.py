# =============================================================================
# vs_notifications / tasks.py
#
# Celery tasks for vs_notifications.
#
# Tasks:
#   deliver_email_notification  — dispatches a single email Notification record
#
# On terminal transition (SENT / FAILED) the task fires the corresponding
# delivery signal (notification_sent / notification_failed) so downstream
# trackers (e.g. vs_user invitation status) can react. The record's internal
# `metadata` carries the correlation data a receiver needs.
# =============================================================================

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.mail import build_from_email, send_email

from .signals import notification_sent, notification_failed

logger = logging.getLogger("vs_notifications.tasks")


@shared_task(bind=True, name="vs_notifications.deliver_email_notification")
def deliver_email_notification(self, notification_id: str):
    """
    Dispatch a single email Notification record via core.mail.send_email so the
    platform's from-address and CC conventions apply. Multipart when the record
    carries an html_body. Transitions status to SENT or FAILED.

    Retry behaviour:
        - Max retries and backoff seconds are read from vs_config at execution
          time so they can be adjusted without redeployment.
        - Retries use a fixed countdown (not exponential).
        - On final failure the record is marked FAILED and the task exits.

    Locking / transactions:
        - The record is fetched, guarded, and bookkept under a single
          transaction.atomic() with select_for_update() (which requires an
          open transaction — the previous code called it outside one, which
          errors on Postgres).
        - The actual SMTP send happens OUTSIDE the row lock: we release the
          lock after reading, send, then re-open a short transaction to write
          the result. This avoids holding a row lock across a slow network call.

    Eager mode:
        - Under CELERY_TASK_ALWAYS_EAGER the task runs in-process inside the
          HTTP request; self.retry() would raise straight through the request.
          The first failure is therefore treated as final (mark FAILED, fire
          notification_failed, no retry raise) — mirroring the old vs_user
          bypass tasks so migrating them onto the engine keeps that guarantee.

    From-address override:
        - When notif.metadata carries a "from_name", the outgoing message's From
          display name is built from it via build_from_email(from_name). This
          preserves the per-message From parity the old bypass paths had (the
          inviter's display name on invitations, sender_name on password resets).

    Idempotency guard:
        - If the notification is already SENT when the task runs, exit without
          re-sending.

    Args:
        notification_id:  UUID string of the Notification record.
    """
    from .models import Notification, NotificationStatus

    # ── Fetch + idempotency guard under a short lock ───────────────────────
    try:
        with transaction.atomic():
            notif = (
                Notification.objects.select_for_update()
                .get(id=notification_id)
            )

            if notif.status == NotificationStatus.SENT:
                logger.debug(
                    "deliver_email_notification: Notification %s already SENT. Skipping.",
                    notification_id,
                )
                return

            email_addr = notif.effective_email
            subject = notif.subject
            body = notif.body
            html_body = notif.html_body
            from_name = (notif.metadata or {}).get("from_name")
    except Notification.DoesNotExist:
        logger.warning(
            "deliver_email_notification: Notification %s not found. Skipping.",
            notification_id,
        )
        return

    # ── No email address — mark FAILED (terminal) and fire the signal ──────
    if not email_addr:
        # Should not normally reach here — dispatch.py catches this pre-flight.
        # Guard defensively for records created by other paths.
        with transaction.atomic():
            notif = Notification.objects.select_for_update().get(id=notification_id)
            notif.status = NotificationStatus.FAILED
            notif.failure_reason = "NO_EMAIL_ADDRESS"
            notif.save(update_fields=["status", "failure_reason"])
        logger.error(
            "deliver_email_notification: Notification %s has no email address. Marked FAILED.",
            notification_id,
        )
        notification_failed.send(sender=Notification, notification=notif)
        return

    max_retries, retry_backoff = _read_retry_config()

    # ── Attempt delivery (outside any row lock) ────────────────────────────
    try:
        send_email(
            subject=subject,
            plain_message=body,
            html_message=html_body or None,
            recipient_list=[email_addr],
            from_email=build_from_email(from_name) if from_name else None,
        )
    except Exception as exc:
        # Record the failed attempt.
        with transaction.atomic():
            notif = Notification.objects.select_for_update().get(id=notification_id)
            notif.retry_count += 1
            notif.failure_reason = str(exc)
            attempts = notif.retry_count
            notif.save(update_fields=["retry_count", "failure_reason"])

        logger.warning(
            "deliver_email_notification: Notification %s failed on attempt %d: %s",
            notification_id, attempts, exc,
        )

        # Eager mode runs in-process during the HTTP request — retrying would
        # raise celery.exceptions.Retry straight through the caller. Treat the
        # first failure as final in that mode.
        if not self.request.is_eager and self.request.retries < max_retries:
            raise self.retry(exc=exc, countdown=retry_backoff)

        # Final failure — mark FAILED (terminal) and fire the signal.
        with transaction.atomic():
            notif = Notification.objects.select_for_update().get(id=notification_id)
            notif.status = NotificationStatus.FAILED
            notif.save(update_fields=["status"])
        logger.error(
            "deliver_email_notification: Notification %s FAILED after %d attempts. "
            "Last error: %s",
            notification_id, attempts, exc,
        )
        notification_failed.send(sender=Notification, notification=notif)
        return

    # ── Success — mark SENT (terminal) and fire the signal ─────────────────
    with transaction.atomic():
        notif = Notification.objects.select_for_update().get(id=notification_id)
        notif.status = NotificationStatus.SENT
        notif.dispatched_at = timezone.now()
        notif.retry_count += 1
        notif.failure_reason = ""
        notif.save(
            update_fields=["status", "dispatched_at", "retry_count", "failure_reason"]
        )

    logger.info(
        "deliver_email_notification: Notification %s SENT to %s (attempt %d).",
        notification_id, email_addr, notif.retry_count,
    )
    notification_sent.send(sender=Notification, notification=notif)


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
