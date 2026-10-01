"""Celery tasks for vs_payments - asynchronous webhook processing.  # Off-request PSP work.

The webhook receiver (:func:`vs_payments.webhooks.ingest_webhook`) stores-and-acks fast,
then enqueues :func:`process_webhook_event` so the outbound PSP re-verify + booking runs
on a worker rather than inside the provider's HTTP callback. ``apps/apps/celery.py`` calls
``autodiscover_tasks()``, so this module is picked up automatically.

Payout dispatch arrives here the same way, and for a stronger reason: a bank transfer
cannot be rolled back, so it must not be made inside the approval transaction. Approval
commits, ``transaction.on_commit`` enqueues :func:`dispatch_payout_batch`, and
:func:`dispatch_undispatched_payout_batches` sweeps up anything the enqueue lost.

Webhook processing is event-driven off the ``transaction.on_commit`` enqueue. The alarm
and sweep tasks below carry beat entries: a booking that fails is recorded and shown, but
nothing tells anyone, so the alarms turn a findable problem into a noticed one, and
:func:`recover_unconfirmed_payments` books the money whose webhook or task never
arrived at all.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("vs_payments.tasks")  # Namespaced logger for payment task diagnostics.


@shared_task(
    bind=True, name="vs_payments.process_webhook_event",
    acks_late=True, reject_on_worker_lost=True,
)
# Handle the process webhook event workflow.
def process_webhook_event(self, event_id: int):
    """Re-verify against the PSP and book the receipt/payout for a stored webhook event.

    Acknowledged only after it finishes (``acks_late``), and handed back to the
    broker if the worker process dies mid-run (``reject_on_worker_lost``). The
    provider already has its 200 and will not resend, so a message acknowledged
    on receipt and then lost with its worker is money nobody books. Re-running is
    safe: the processor no-ops on a PROCESSED event and the confirm services
    never book twice.
    """
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


@shared_task(name="vs_payments.dispatch_payout_batch")
# Dispatch one approved payout batch to the provider.
def dispatch_payout_batch(batch_id: int, instance_id, actor_user_id=None):
    """Send an approved batch to the PSP, outside the transaction that approved it.

    Enqueued from :meth:`vs_payments.workflow_handlers.PayoutBatchApprovalHandler.on_approved`
    via ``transaction.on_commit``, so it can only run against an approval that is
    already durable. Idempotent: the approval is re-validated and each instruction
    claims itself before its own send, so a task retry cannot pay a beneficiary twice.
    """
    from .services import dispatch_approved_payout_batch

    dispatch_approved_payout_batch(
        batch_id=batch_id, instance_id=instance_id, actor_user_id=actor_user_id,
    )


@shared_task(name="vs_payments.dispatch_undispatched_payout_batches")
# Re-dispatch approved batches whose hand-off never ran.
def dispatch_undispatched_payout_batches():
    """Recovery sweep for approvals that committed but never reached the provider."""
    from .services import sweep_undispatched_payout_batches

    summary = sweep_undispatched_payout_batches()  # Idempotent; a quiet run finds nothing.
    if summary["dispatched"] or summary["failures"] or summary["skipped"]:
        logger.warning("dispatch_undispatched_payout_batches: %s", summary)
    return summary


@shared_task(
    name="vs_payments.recover_unconfirmed_payments",
    acks_late=True, reject_on_worker_lost=True,
)
# Book settled money whose webhook or processing task never arrived.
def recover_unconfirmed_payments():
    """Re-run lost webhook events and re-verify unconfirmed collections and payouts.

    See :mod:`vs_payments.recovery`. Late acknowledgement for the same reason as
    :func:`process_webhook_event`: this task books money, and every step of it is
    idempotent, so a re-run after a lost worker costs provider calls and nothing
    else.
    """
    from .recovery import recover_unconfirmed_payments as sweep

    summary = sweep()
    if summary["collections_booked"] or summary["payouts_booked"] or summary["failures"]:
        logger.warning("recover_unconfirmed_payments: %s", summary)
    return summary


@shared_task(name="vs_payments.run_held_settlements", acks_late=True, reject_on_worker_lost=True)
# Pay each held-mode branch what the platform holds for it, on its tenant's interval.
def run_held_settlements():
    """Build every settlement that is due, then post platform journals left waiting.

    See :func:`vs_payments.held.run_settlements`. Idempotent per branch and day: a
    branch with a settlement still pending, or settled within its tenant's
    interval, is left alone, and a payment is claimed by one settlement only.
    """
    from .held import post_pending_platform_journals, run_settlements

    summary = run_settlements()
    summary["platform_journals"] = post_pending_platform_journals()
    if summary["built"] or summary["platform_journals"]["waiting"]:
        logger.info("run_held_settlements: %s", summary)
    return summary


@shared_task(name="vs_payments.apply_custody_switches", acks_late=True, reject_on_worker_lost=True)
# Apply custody changes whose month has come, or say why they wait.
def apply_custody_switches():
    """See :func:`vs_payments.held.apply_custody_switches`. Safe to re-run."""
    from .held import apply_custody_switches as apply

    summary = apply()
    if summary["results"]:
        logger.info("apply_custody_switches: %s", summary)
    return summary
