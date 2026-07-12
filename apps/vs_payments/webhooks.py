"""Inbound webhook ingestion — verify, deduplicate, store, dispatch.

The single entry point :func:`ingest_webhook` is the only thing the webhook view calls.
It enforces the two non-negotiables of PSP webhooks:

1. **Authenticity** — the raw body's signature must verify against the provider secret,
   else we reject (401) and never act on it.
2. **Idempotency** — every event is recorded under a unique ``dedupe_key``; a provider
   retrying the same event finds the row already present and does nothing. The downstream
   ``confirm_*`` services are *also* idempotent (terminal-state short-circuit), so a
   duplicate can never book a second receipt/payout even under a race.

The raw body and headers are persisted verbatim before any processing, so an event is
always auditable/replayable regardless of how dispatch goes.
"""
from __future__ import annotations

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from . import audit, services
from .constants import (
    PaymentAuditAction,
    PaymentDirection,
    WebhookStatus,
)
from .exceptions import DuplicateWebhookError, WebhookSignatureError
from .models import CollectionIntent, PayoutInstruction, WebhookEvent
from .providers.registry import get_provider


# Handle the ingest webhook workflow.
def ingest_webhook(*, provider: str, raw_body: bytes, headers: dict | None = None) -> WebhookEvent:
    """Verify and store one inbound webhook, then hand processing to a background task.

    This is the *fast, synchronous* half of the receiver: verify the signature, persist
    the event verbatim under its idempotency key, audit its arrival, and enqueue the
    re-verify/book step for a Celery worker. The PSP only needs a prompt 200 ack — the
    outbound re-verification (an extra provider round-trip) happens off the request path
    in :func:`process_stored_event` via ``vs_payments.process_webhook_event``.

    Returns the stored event (now ``RECEIVED``; the worker flips it to
    ``PROCESSED``/``IGNORED``/``FAILED``). Raises :class:`WebhookSignatureError` (401) on
    a bad signature and :class:`DuplicateWebhookError` (200) when the event was already
    fully processed.
    """
    headers = headers or {}  # Treat missing headers as an empty mapping.
    provider = provider.upper()  # Normalize the provider name for lookup and storage.
    client = get_provider(provider)  # Resolve the provider adapter before touching the payload.

    if not client.verify_signature(raw_body=raw_body, headers=headers):  # Reject events that fail authenticity checks.
        audit.record(  # Write a rejection event so signature failures are visible in audit logs.
            action=PaymentAuditAction.WEBHOOK_REJECTED, provider=provider, succeeded=False,
            message="Signature verification failed.",  # entity stays None: the payload is untrusted here, so we can't attribute one.
        )
        raise WebhookSignatureError(provider=provider)

    try:  # Providers occasionally send malformed JSON bodies even when the signature is valid.
        payload = json.loads(raw_body or b"{}")  # Parse the body for provider-specific interpretation.
    except json.JSONDecodeError:  # Fall back to an empty payload if the body is not valid JSON.
        payload = {}
    parsed = client.parse_webhook(payload=payload, raw_body=raw_body, headers=headers)  # Normalize provider-specific fields.
    dedupe_key = parsed.dedupe_key or f"{provider}:{hashlib.sha256(raw_body).hexdigest()}"  # Build a stable fallback idempotency key.

    # Persist-or-find atomically; the unique dedupe_key is the idempotency backbone.
    event, created = WebhookEvent.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults=dict(  # Store the raw inbound event data on first sight.
            provider=provider, event_type=parsed.event_type,  # Record who sent it and what it claims to be.
            provider_reference=parsed.provider_reference or parsed.reference,  # Preserve the provider-side lookup key.
            signature=_signature(headers), verified=True,  # Capture the signature and the verification result.
            status=WebhookStatus.RECEIVED, headers=_jsonable(headers),  # Store metadata with the initial received state.
            payload=payload, raw_body=raw_body.decode("utf-8", "replace"),  # Keep both parsed and raw representations.
        ),
    )
    if not created and event.status == WebhookStatus.PROCESSED:  # A processed event is a true duplicate retry.
        raise DuplicateWebhookError()

    if created:  # Audit-once: only the first sighting emits a WEBHOOK_RECEIVED row (a not-yet-processed
        record = _find_record(parsed)  # retry re-enters here but must not add a second audit line). Resolve the target once
        audit.record(  # so the audit row is attributed to the matched record's entity (shows in its log).
            action=PaymentAuditAction.WEBHOOK_RECEIVED, provider=provider,
            entity=getattr(record, "entity", None),  # Attribute the event to the matched record's entity.
            reference=parsed.reference, message=f"{parsed.event_type} ({parsed.direction}).",
        )

    # Defer the outbound re-verify + booking to a worker; on_commit ensures a rolled-back
    # store never enqueues a phantom event, and confirm_* stay idempotent under re-delivery.
    transaction.on_commit(lambda: _enqueue(event.id))
    return event  # Return the stored (RECEIVED) event; the task moves it to a terminal state.


# Support the enqueue workflow.
def _enqueue(event_id: int) -> None:
    """Fire the async processing task for a stored event (local import avoids a cycle)."""
    from .tasks import process_webhook_event  # Import here so tasks.py can import webhooks at module load.
    process_webhook_event.delay(event_id)  # Under ALWAYS_EAGER this runs inline; in prod a worker picks it up.


# Handle the process stored event workflow.
def process_stored_event(event_id: int) -> WebhookEvent | None:
    """Re-verify against the PSP and book the receipt/payout for a stored webhook event.

    Idempotent by design: a missing or already-``PROCESSED`` event is a no-op, so a task
    retry (or a provider re-delivery that lands on the same row) can never double-book.
    Runs the same :func:`_dispatch` the synchronous path used to; on failure the event is
    marked ``FAILED`` and the exception is *swallowed* — mirroring the platform's
    "eager-mode first failure is final": the PSP re-delivers and ``confirm_*`` are
    idempotent, so re-raising would only surface a spurious 500 to the (already-acked) PSP.
    """
    event = WebhookEvent.objects.filter(pk=event_id).first()  # Load the stored event, if it still exists.
    if event is None or event.status == WebhookStatus.PROCESSED:  # Nothing to do for a gone/handled event.
        return event  # Idempotent no-op on re-entry.

    client = get_provider(event.provider)  # Resolve the adapter that stored this event.
    parsed = client.parse_webhook(  # Re-derive the neutral view from the persisted body.
        payload=event.payload or {},
        raw_body=(event.raw_body or "").encode(),  # Rebuild the raw bytes the parser may inspect.
        headers=event.headers or {},
    )
    record = _find_record(parsed)  # Resolve the target collection/payout for dispatch.

    try:  # Dispatch can fail after the webhook is safely stored.
        _dispatch(event, parsed, record)
    except Exception as exc:  # Processing failed, but the event stays stored for replay/debugging.
        event.status = WebhookStatus.FAILED  # Mark the event failed so it can be retried explicitly.
        event.error = str(getattr(exc, "message", exc))[:255]  # Keep a short error string for operators.
        event.save(update_fields=["status", "error", "updated_at"])
        # Deliberately do NOT re-raise: the PSP is already acked and confirm_* are idempotent.
    return event  # Return the event in its (now terminal) state.


# Support the dispatch workflow.
def _dispatch(event: WebhookEvent, parsed, record=None) -> None:
    """Route a verified event to the matching confirm service and mark it processed.

    SECURITY: a valid signature proves the event *came from* the provider, but we do
    **not** trust the status/amount it carries to move money. The event tells us only
    *which* transaction changed; the ``confirm_*`` services then re-verify the
    authoritative status and settled amount against the provider's API (the
    ``status=None`` path polls ``verify_collection`` / ``verify_transfer``) before
    booking any receipt or payout. This defends against a premature/forged-but-signed
    ``success`` (e.g. Paystack sets ``charge.success`` regardless of the inner txn
    status) and against a leaked webhook secret being used to fabricate settlements.
    """
    if parsed.direction == PaymentDirection.COLLECTION:  # Money-in events are matched to collection intents.
        intent = record  # Reuse the record resolved during ingestion to avoid a second lookup.
        if intent is not None:  # Only confirm if the webhook maps to a known intent.
            services.confirm_collection(intent)  # Re-verify the provider state before booking the receipt.
            event.collection = intent  # Link the webhook event to the matching collection.
            event.status = WebhookStatus.PROCESSED  # Mark the webhook as fully handled.
        else:  # If we cannot resolve the intent, we leave the event stored but unprocessed.
            event.status = WebhookStatus.IGNORED  # Record that the payload was valid but unmatched.
            event.error = "No matching collection intent."  # Save a clear operator-facing explanation.
    elif parsed.direction == PaymentDirection.PAYOUT:  # Money-out events are matched to payout instructions.
        payout = record  # Reuse the record resolved during ingestion to avoid a second lookup.
        if payout is not None:  # Only confirm if the webhook maps to a known payout.
            services.confirm_payout(payout)  # Re-verify the provider state before posting the vendor payment.
            event.payout = payout  # Link the webhook event to the matching payout.
            event.status = WebhookStatus.PROCESSED  # Mark the webhook as fully handled.
        else:  # If we cannot resolve the payout, keep the webhook as an ignored audit record.
            event.status = WebhookStatus.IGNORED  # Record that the payload was valid but unmatched.
            event.error = "No matching payout instruction."  # Save a clear operator-facing explanation.
    else:  # Unknown event directions are stored but not acted on.
        event.status = WebhookStatus.IGNORED  # Mark the event ignored rather than failing it.
        event.error = f"Unhandled direction '{parsed.direction}'."  # Preserve the unsupported direction for debugging.

    event.processed_at = timezone.now()
    event.save(update_fields=[
        "collection", "payout", "status", "error", "processed_at", "updated_at",
    ])


# Support the find record workflow.
def _find_record(parsed):
    """Resolve the local collection/payout this event targets (or None if unmatched).

    Resolving once here lets us attribute the WEBHOOK_RECEIVED audit row to the record's
    entity and hand the same object to :func:`_dispatch` without a second query.
    """
    if parsed.direction == PaymentDirection.COLLECTION:  # Money-in events map to a collection intent.
        return _find_collection(parsed)
    if parsed.direction == PaymentDirection.PAYOUT:  # Money-out events map to a payout instruction.
        return _find_payout(parsed)
    return None  # Unknown direction has no attributable record.


# Support the find collection workflow.
def _find_collection(parsed):
    qs = CollectionIntent.objects.all()
    if parsed.reference:  # Prefer the merchant/provider reference when present.
        intent = qs.filter(reference=parsed.reference).first()
        if intent:  # Return immediately on an exact match.
            return intent
    if parsed.provider_reference:  # Fall back to the PSP reference if needed.
        return qs.filter(provider_reference=parsed.provider_reference).first()
    return None  # No local collection matched the webhook.


# Support the find payout workflow.
def _find_payout(parsed):
    qs = PayoutInstruction.objects.all()
    if parsed.reference:  # Prefer the merchant/provider reference when present.
        payout = qs.filter(reference=parsed.reference).first()
        if payout:  # Return immediately on an exact match.
            return payout
    if parsed.provider_reference:  # Fall back to the PSP reference if the merchant reference is missing.
        return qs.filter(provider_reference=parsed.provider_reference).first()
    return None  # No local payout matched the webhook.


# Support the signature workflow.
def _signature(headers: dict) -> str:
    for key in ("x-paystack-signature", "Authorization", "x-fake-signature"):  # Check the known signature header names.
        for hk, hv in (headers or {}).items():  # Walk the received header mapping.
            if hk.lower() == key.lower():  # Compare case-insensitively because header casing varies by server.
                return str(hv)[:256]  # Store only a bounded signature string.
    return ""  # No known signature header was present.


# Support the jsonable workflow.
def _jsonable(headers: dict) -> dict:
    return {str(k): str(v) for k, v in (headers or {}).items()}  # Normalize headers into JSON-safe strings.
