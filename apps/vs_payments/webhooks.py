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


def ingest_webhook(*, provider: str, raw_body: bytes, headers: dict | None = None) -> WebhookEvent:
    """Verify, store and process one inbound webhook. Returns the stored event.

    Raises :class:`WebhookSignatureError` (401) on a bad signature and
    :class:`DuplicateWebhookError` (200) when the event was already handled.
    """
    headers = headers or {}
    provider = provider.upper()
    client = get_provider(provider)

    if not client.verify_signature(raw_body=raw_body, headers=headers):
        audit.record(
            action=PaymentAuditAction.WEBHOOK_REJECTED, provider=provider, succeeded=False,
            message="Signature verification failed.",
        )
        raise WebhookSignatureError(provider=provider)

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        payload = {}
    parsed = client.parse_webhook(payload=payload, raw_body=raw_body, headers=headers)
    dedupe_key = parsed.dedupe_key or f"{provider}:{hashlib.sha256(raw_body).hexdigest()}"

    # Persist-or-find atomically; the unique dedupe_key is the idempotency backbone.
    event, created = WebhookEvent.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults=dict(
            provider=provider, event_type=parsed.event_type,
            provider_reference=parsed.provider_reference or parsed.reference,
            signature=_signature(headers), verified=True,
            status=WebhookStatus.RECEIVED, headers=_jsonable(headers),
            payload=payload, raw_body=raw_body.decode("utf-8", "replace"),
        ),
    )
    if not created and event.status == WebhookStatus.PROCESSED:
        raise DuplicateWebhookError()

    audit.record(
        action=PaymentAuditAction.WEBHOOK_RECEIVED, provider=provider,
        reference=parsed.reference, message=f"{parsed.event_type} ({parsed.direction}).",
    )

    try:
        _dispatch(event, parsed)
    except Exception as exc:  # processing failed — keep the event for replay
        event.status = WebhookStatus.FAILED
        event.error = str(getattr(exc, "message", exc))[:255]
        event.save(update_fields=["status", "error", "updated_at"])
        raise

    return event


def _dispatch(event: WebhookEvent, parsed) -> None:
    """Route a verified event to the matching confirm service and mark it processed."""
    if parsed.direction == PaymentDirection.COLLECTION:
        intent = _find_collection(parsed)
        if intent is not None:
            services.confirm_collection(intent, status=parsed.status)
            event.collection = intent
            event.status = WebhookStatus.PROCESSED
        else:
            event.status = WebhookStatus.IGNORED
            event.error = "No matching collection intent."
    elif parsed.direction == PaymentDirection.PAYOUT:
        payout = _find_payout(parsed)
        if payout is not None:
            services.confirm_payout(payout, status=parsed.status)
            event.payout = payout
            event.status = WebhookStatus.PROCESSED
        else:
            event.status = WebhookStatus.IGNORED
            event.error = "No matching payout instruction."
    else:
        event.status = WebhookStatus.IGNORED
        event.error = f"Unhandled direction '{parsed.direction}'."

    event.processed_at = timezone.now()
    event.save(update_fields=[
        "collection", "payout", "status", "error", "processed_at", "updated_at",
    ])


def _find_collection(parsed):
    qs = CollectionIntent.objects.all()
    if parsed.reference:
        intent = qs.filter(reference=parsed.reference).first()
        if intent:
            return intent
    if parsed.provider_reference:
        return qs.filter(provider_reference=parsed.provider_reference).first()
    return None


def _find_payout(parsed):
    qs = PayoutInstruction.objects.all()
    if parsed.reference:
        payout = qs.filter(reference=parsed.reference).first()
        if payout:
            return payout
    if parsed.provider_reference:
        return qs.filter(provider_reference=parsed.provider_reference).first()
    return None


def _signature(headers: dict) -> str:
    for key in ("x-paystack-signature", "Authorization", "x-fake-signature"):
        for hk, hv in (headers or {}).items():
            if hk.lower() == key.lower():
                return str(hv)[:256]
    return ""


def _jsonable(headers: dict) -> dict:
    return {str(k): str(v) for k, v in (headers or {}).items()}
