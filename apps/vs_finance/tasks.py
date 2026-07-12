"""Celery tasks for vs_finance.

Currently home to the daily dunning run: the scheduled job that makes automated
reminders actually *active*. It generates the day's dunning notices and dispatches
every PENDING notice through **vs_notifications** (delivery never leaves vs_finance
directly — see :func:`vs_finance.dunning.mark_notice_sent`).

Autodiscovered by Celery via ``app.autodiscover_tasks()`` in ``apps/apps/celery.py``
(which scans ``tasks`` in every installed app); wired to beat as
``finance-daily-dunning``.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)  # Module logger for dunning task events.


def queue_payment_received_notification(payment_id, *, actor_user_id=None):
    """Enqueue receipt-email creation without delaying or breaking payment posting."""
    try:
        send_payment_received_notification.delay(
            payment_id, actor_user_id=actor_user_id,
            _job_owner_id=str(actor_user_id) if actor_user_id else None,
            _job_label="Receipt email",
            _job_kind="email",
        )
        return True
    except Exception:  # Broker availability must never change a posted receipt.
        logger.exception(
            "Could not enqueue payment-received notification for payment %s", payment_id,
        )
        return False


@shared_task(name="vs_finance.send_payment_received_notification")
def send_payment_received_notification(payment_id, *, actor_user_id=None):
    """Create the receipt notification; vs_notifications owns delivery and status."""
    from django.contrib.auth import get_user_model

    from .models import Payment
    from .notifications import notify_payment_received

    payment = Payment.objects.select_related(
        "customer", "entity__source_school",
    ).filter(pk=payment_id).first()
    if payment is None:
        logger.warning("Receipt email skipped; payment %s no longer exists", payment_id)
        return {"queued": False, "reason": "payment_not_found"}

    actor_user = None
    if actor_user_id:
        actor_user = get_user_model().objects.filter(pk=actor_user_id).first()
    notification_ids = notify_payment_received(payment, actor_user=actor_user) or []
    return {"queued": bool(notification_ids), "notification_ids": notification_ids}


@shared_task(name="vs_finance.run_daily_dunning")
# Generate and dispatch daily finance dunning notices.
def run_daily_dunning():
    """Generate and dispatch dunning reminders for every school-owned entity.

    For each active :class:`~vs_finance.models.LedgerEntity` that maps to a school
    (platform/product books have no school to scope notifications to and are skipped),
    this:

      1. runs :func:`~vs_finance.dunning.generate_dunning` — creating the day's new
         PENDING notices (skips the entity, logging, if it has no active policy); then
      2. dispatches **all** the entity's PENDING notices via
         :func:`~vs_finance.dunning.mark_notice_sent`, which delivers through
         vs_notifications and flips each to SENT.

    Every entity and every notice is wrapped so one failure never aborts the run.
    Returns a ``{"generated": N, "sent": N, "skipped": N}`` summary.
    """
    from .constants import DunningNoticeStatus
    from .dunning import generate_dunning, mark_notice_sent
    from .models import DunningNotice, LedgerEntity

    generated = 0  # Count notices created during this run.
    sent = 0  # Count pending notices successfully dispatched.
    skipped = 0  # Count entities skipped because generation failed.

    entities = LedgerEntity.objects.filter(is_active=True, source_school__isnull=False)
    for entity in entities:  # Treat each entity independently so one failure does not abort the run.
        try:  # Generation can fail for entity-specific policy/configuration issues.
            created = generate_dunning(entity)  # Create today's new pending notices.
            generated += len(created)  # Add generated notice count to summary.
        except Exception as exc:  # noqa: BLE001 - no policy / config; log and skip entity
            skipped += 1  # Track that this entity could not generate notices.
            logger.info(  # Log at info because missing policy/config can be expected.
                "run_daily_dunning: skipping entity %s (%s) — %s",  # Include entity code, id, and reason.
                entity.code, entity.id, exc,  # Log context values.
            )
            # Still attempt to dispatch any pre-existing PENDING notices below.  # Dispatch is independent of generation.

        pending = DunningNotice.objects.filter(
            entity=entity, notice_status=DunningNoticeStatus.PENDING,  # Scope to pending notices only.
        )
        for notice in pending:  # Dispatch each notice independently.
            try:  # A single bad notice should not abort later notices.
                mark_notice_sent(notice)  # Send the notice through the dunning delivery helper.
                sent += 1  # Count successful dispatches.
            except Exception as exc:  # noqa: BLE001 - one bad notice must not abort the run
                logger.warning(  # Warn because this notice failed after generation.
                    "run_daily_dunning: failed to dispatch notice %s (entity %s) — %s",  # Include notice and entity context.
                    notice.document_number or notice.pk, entity.code, exc,  # Prefer document number, fallback to pk.
                )

    logger.info(  # Emit one summary event for monitoring.
        "run_daily_dunning complete — generated=%d, sent=%d, skipped=%d",  # Summary log template.
        generated, sent, skipped,  # Summary counts.
    )
    return {"generated": generated, "sent": sent, "skipped": skipped}  # Return task result for Celery history.
