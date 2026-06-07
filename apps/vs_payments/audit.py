"""Durable gateway-action logging for vs_payments.

Thin wrapper over :class:`vs_payments.models.PaymentEvent` (append-only, immutable). The
ledger postings done downstream already write their own ``vs_finance`` audit rows; this
captures the gateway-side actions (initiation, confirmation, webhook receipt) and, just
as importantly, **rejected** attempts — written in their own committed transaction so a
rollback of the failed action doesn't erase the record of it.
"""
from __future__ import annotations

from django.db import transaction


def record(*, action, entity=None, provider="", reference="", succeeded=True,
           message="", metadata=None, actor_user=None):
    """Write one immutable :class:`PaymentEvent`. Returns it (or None on failure)."""
    from .models import PaymentEvent

    try:
        return PaymentEvent.objects.create(
            entity=entity, provider=provider or "", action=action,
            reference=reference or "", succeeded=succeeded,
            message=(message or "")[:255], metadata=metadata or {}, actor_user=actor_user,
        )
    except Exception:  # pragma: no cover - audit must never break the caller
        return None


def record_rejection(*, action, exc, entity=None, provider="", reference="",
                     metadata=None, actor_user=None):
    """Log a FAILED gateway action in its OWN committed transaction (survives rollback)."""
    from .models import PaymentEvent

    payload = dict(metadata or {})
    payload.setdefault("error_code", getattr(exc, "error_code", "ERROR"))
    try:
        with transaction.atomic():
            return PaymentEvent.objects.create(
                entity=entity, provider=provider or "", action=action,
                reference=reference or "", succeeded=False,
                message=str(getattr(exc, "message", exc))[:255],
                metadata=payload, actor_user=actor_user,
            )
    except Exception:  # pragma: no cover
        return None
