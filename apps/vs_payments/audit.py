"""Durable gateway-action logging for vs_payments.  # Append-only audit trail for gateway actions.

Thin wrapper over :class:`vs_payments.models.PaymentEvent` (append-only, immutable). The
ledger postings done downstream already write their own ``vs_finance`` audit rows; this
captures the gateway-side actions (initiation, confirmation, webhook receipt) and, just
as importantly, **rejected** attempts — written in their own committed transaction so a
rollback of the failed action doesn't erase the record of it.  # Keep gateway events durable even on rollback.
"""
from __future__ import annotations

from django.db import transaction


def record(*, action, entity=None, provider="", reference="", succeeded=True,
           message="", metadata=None, actor_user=None):
    """Write one immutable :class:`PaymentEvent`. Returns it (or None on failure)."""
    from .models import PaymentEvent

    try:  # Audit writes should never block the caller.
        return PaymentEvent.objects.create(
            entity=entity, provider=provider or "", action=action,  # Store the source of the event.
            reference=reference or "", succeeded=succeeded,  # Save the reference and success state.
            message=(message or "")[:255], metadata=metadata or {}, actor_user=actor_user,  # Keep the payload bounded.
        )
    except Exception:  # pragma: no cover - audit must never break the caller
        return None  # Swallow audit failures so business logic can continue.


def record_rejection(*, action, exc, entity=None, provider="", reference="",
                     metadata=None, actor_user=None):
    """Log a FAILED gateway action in its OWN committed transaction (survives rollback)."""
    from .models import PaymentEvent

    payload = dict(metadata or {})  # Copy the caller metadata so we can add error details.
    payload.setdefault("error_code", getattr(exc, "error_code", "ERROR"))  # Ensure there is a stable error code.
    try:  # Rejection logs should still fail safely if the database write itself fails.
        with transaction.atomic():
            return PaymentEvent.objects.create(
                entity=entity, provider=provider or "", action=action,  # Store the source of the failure.
                reference=reference or "", succeeded=False,  # Rejections are always failed by definition.
                message=str(getattr(exc, "message", exc))[:255],  # Keep the human-readable message short.
                metadata=payload, actor_user=actor_user,  # Persist the error payload and actor.
            )
    except Exception:  # pragma: no cover
        return None  # Never let an audit write failure bubble up.
