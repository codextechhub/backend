"""Celery tasks for vs_payments - asynchronous webhook processing.  # Off-request PSP work.

The webhook receiver (:func:`vs_payments.webhooks.ingest_webhook`) stores-and-acks fast,
then enqueues :func:`process_webhook_event` so the outbound PSP re-verify + booking runs
on a worker rather than inside the provider's HTTP callback. ``apps/apps/celery.py`` calls
``autodiscover_tasks()``, so this module is picked up automatically.

Webhook processing itself is purely event-driven off the ``transaction.on_commit``
enqueue. The two alarm tasks below are the exception and do carry beat entries: a
booking that fails is recorded and shown, but nothing tells anyone, so they are what
turns a findable problem into a noticed one.  # Keep the request path fast.
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


@shared_task(name="vs_payments.alert_unbooked_receipts")
# Report each entity's outstanding unbooked provider events.
def alert_unbooked_receipts():
    """Daily standing report of money that arrived and did not reach the books."""
    from .alerts import unbooked_digest

    summary = unbooked_digest()  # One message per entity, never one per event.
    if summary["events"]:  # Stay silent on a clean day rather than logging noise.
        logger.info("alert_unbooked_receipts: %s", summary)
    return summary


@shared_task(name="vs_payments.alert_unbooked_surge")
# Alarm platform staff when bookings start failing in bulk.
def alert_unbooked_surge():
    """Incident alarm: several bookings failing in one window is a systemic cause."""
    from .alerts import unbooked_surge

    summary = unbooked_surge()  # Windowed, so a resolved outage goes quiet by itself.
    if summary["alarmed"]:  # Only worth a log line when it actually fired.
        logger.warning("alert_unbooked_surge: %s", summary)
    return summary
