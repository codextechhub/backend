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

logger = logging.getLogger(__name__)


@shared_task(name="vs_finance.run_daily_dunning")
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

    generated = 0
    sent = 0
    skipped = 0

    entities = LedgerEntity.objects.filter(is_active=True, source_school__isnull=False)
    for entity in entities:
        try:
            created = generate_dunning(entity)
            generated += len(created)
        except Exception as exc:  # noqa: BLE001 - no policy / config; log and skip entity
            skipped += 1
            logger.info(
                "run_daily_dunning: skipping entity %s (%s) — %s",
                entity.code, entity.id, exc,
            )
            # Still attempt to dispatch any pre-existing PENDING notices below.

        pending = DunningNotice.objects.filter(
            entity=entity, notice_status=DunningNoticeStatus.PENDING,
        )
        for notice in pending:
            try:
                mark_notice_sent(notice)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - one bad notice must not abort the run
                logger.warning(
                    "run_daily_dunning: failed to dispatch notice %s (entity %s) — %s",
                    notice.document_number or notice.pk, entity.code, exc,
                )

    logger.info(
        "run_daily_dunning complete — generated=%d, sent=%d, skipped=%d",
        generated, sent, skipped,
    )
    return {"generated": generated, "sent": sent, "skipped": skipped}
