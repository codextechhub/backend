"""Celery tasks for vs_payments — asynchronous webhook processing.  # Off-request PSP work.

The webhook receiver (:func:`vs_payments.webhooks.ingest_webhook`) stores-and-acks fast,
then enqueues :func:`process_webhook_event` so the outbound PSP re-verify + booking runs
on a worker rather than inside the provider's HTTP callback. ``apps/apps/celery.py`` calls
``autodiscover_tasks()``, so this module is picked up automatically — no beat entry, it is
purely event-driven off the ``transaction.on_commit`` enqueue.  # Keep the request path fast.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("vs_payments.tasks")  # Namespaced logger for payment task diagnostics.


@shared_task(bind=True, name="vs_payments.process_webhook_event")
# Handle the process webhook event workflow.
def process_webhook_event(self, event_id: int):
    """Re-verify against the PSP and book the receipt/payout for a stored webhook event."""
    from .webhooks import process_stored_event  # Local import keeps task discovery cheap and cycle-free.
    process_stored_event(event_id)  # Idempotent: a missing/already-processed event is a no-op.
