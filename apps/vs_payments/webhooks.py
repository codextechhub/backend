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
from __future__ import annotations  # Keep annotation evaluation deferred for readability and compatibility.

import hashlib  # Used to derive a stable fallback dedupe key from the raw body.
import json  # Used to parse webhook payload bodies when they are JSON.

from django.db import transaction  # Wrap webhook dispatch in a transaction when writing state.
from django.utils import timezone  # Timestamp webhook processing consistently.

from . import audit, services  # Audit the inbound event and dispatch it to the payment service layer.
from .constants import (
    PaymentAuditAction,  # Audit action identifiers for webhook lifecycle events.
    PaymentDirection,  # Direction distinguishes collections from payouts.
    WebhookStatus,  # Local webhook state machine.
)
from .exceptions import DuplicateWebhookError, WebhookSignatureError  # Error types surfaced to the caller.
from .models import CollectionIntent, PayoutInstruction, WebhookEvent  # Models used to resolve and persist webhook work.
from .providers.registry import get_provider  # Resolve the configured PSP provider implementation.


def ingest_webhook(*, provider: str, raw_body: bytes, headers: dict | None = None) -> WebhookEvent:
    """Verify, store and process one inbound webhook. Returns the stored event.

    Raises :class:`WebhookSignatureError` (401) on a bad signature and
    :class:`DuplicateWebhookError` (200) when the event was already handled.
    """
    headers = headers or {}  # Treat missing headers as an empty mapping.
    provider = provider.upper()  # Normalize the provider name for lookup and storage.
    client = get_provider(provider)  # Resolve the provider adapter before touching the payload.

    if not client.verify_signature(raw_body=raw_body, headers=headers):  # Reject events that fail authenticity checks.
        audit.record(  # Write a rejection event so signature failures are visible in audit logs.
            action=PaymentAuditAction.WEBHOOK_REJECTED, provider=provider, succeeded=False,
            message="Signature verification failed.",
        )
        raise WebhookSignatureError(provider=provider)  # Surface a 401-style failure to the caller.

    try:  # Providers occasionally send malformed JSON bodies even when the signature is valid.
        payload = json.loads(raw_body or b"{}")  # Parse the body for provider-specific interpretation.
    except json.JSONDecodeError:  # Fall back to an empty payload if the body is not valid JSON.
        payload = {}
    parsed = client.parse_webhook(payload=payload, raw_body=raw_body, headers=headers)  # Normalize provider-specific fields.
    dedupe_key = parsed.dedupe_key or f"{provider}:{hashlib.sha256(raw_body).hexdigest()}"  # Build a stable fallback idempotency key.

    # Persist-or-find atomically; the unique dedupe_key is the idempotency backbone.
    event, created = WebhookEvent.objects.get_or_create(  # Reuse the existing event when the PSP retries the webhook.
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
        raise DuplicateWebhookError()  # Signal the caller that nothing new happened.

    audit.record(  # Record that a valid webhook arrived, even if later dispatch ignores it.
        action=PaymentAuditAction.WEBHOOK_RECEIVED, provider=provider,
        reference=parsed.reference, message=f"{parsed.event_type} ({parsed.direction}).",
    )

    try:  # Dispatch can fail after the webhook is safely stored.
        _dispatch(event, parsed)
    except Exception as exc:  # Processing failed, but the event should remain stored for replay/debugging.
        event.status = WebhookStatus.FAILED  # Mark the event failed so it can be retried explicitly.
        event.error = str(getattr(exc, "message", exc))[:255]  # Keep a short error string for operators.
        event.save(update_fields=["status", "error", "updated_at"])  # Persist the failure state.
        raise

    return event  # Return the stored webhook event after processing.


def _dispatch(event: WebhookEvent, parsed) -> None:
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
        intent = _find_collection(parsed)  # Resolve the local collection record from provider references.
        if intent is not None:  # Only confirm if the webhook maps to a known intent.
            services.confirm_collection(intent)  # Re-verify the provider state before booking the receipt.
            event.collection = intent  # Link the webhook event to the matching collection.
            event.status = WebhookStatus.PROCESSED  # Mark the webhook as fully handled.
        else:  # If we cannot resolve the intent, we leave the event stored but unprocessed.
            event.status = WebhookStatus.IGNORED  # Record that the payload was valid but unmatched.
            event.error = "No matching collection intent."  # Save a clear operator-facing explanation.
    elif parsed.direction == PaymentDirection.PAYOUT:  # Money-out events are matched to payout instructions.
        payout = _find_payout(parsed)  # Resolve the local payout record from provider references.
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

    event.processed_at = timezone.now()  # Stamp the completion time regardless of outcome.
    event.save(update_fields=[  # Persist all changed linkage and status fields.
        "collection", "payout", "status", "error", "processed_at", "updated_at",
    ])


def _find_collection(parsed):
    qs = CollectionIntent.objects.all()  # Start with all collection intents in the current entity scope.
    if parsed.reference:  # Prefer the merchant/provider reference when present.
        intent = qs.filter(reference=parsed.reference).first()  # Look up by our own idempotency key first.
        if intent:  # Return immediately on an exact match.
            return intent
    if parsed.provider_reference:  # Fall back to the PSP reference if needed.
        return qs.filter(provider_reference=parsed.provider_reference).first()  # Resolve by provider transaction id.
    return None  # No local collection matched the webhook.


def _find_payout(parsed):
    qs = PayoutInstruction.objects.all()  # Start with all payout instructions in scope.
    if parsed.reference:  # Prefer the merchant/provider reference when present.
        payout = qs.filter(reference=parsed.reference).first()  # Look up by our own payout reference first.
        if payout:  # Return immediately on an exact match.
            return payout
    if parsed.provider_reference:  # Fall back to the PSP reference if the merchant reference is missing.
        return qs.filter(provider_reference=parsed.provider_reference).first()  # Resolve by provider transaction id.
    return None  # No local payout matched the webhook.


def _signature(headers: dict) -> str:
    for key in ("x-paystack-signature", "Authorization", "x-fake-signature"):  # Check the known signature header names.
        for hk, hv in (headers or {}).items():  # Walk the received header mapping.
            if hk.lower() == key.lower():  # Compare case-insensitively because header casing varies by server.
                return str(hv)[:256]  # Store only a bounded signature string.
    return ""  # No known signature header was present.


def _jsonable(headers: dict) -> dict:
    return {str(k): str(v) for k, v in (headers or {}).items()}  # Normalize headers into JSON-safe strings.
