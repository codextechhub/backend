"""Payment provider webhooks past their period, for :mod:`core.pruning`.

A webhook row keeps the provider's whole message: headers, body and the payer
details inside it. Once handled, the money it reported lives on in the
collection, payout or refund it was booked to (each kept with the books), so a
year on the raw message is only a copy.

A webhook still ``RECEIVED`` is kept whatever its age: recovery retries it and
the unbooked-money alerts read it.
"""
from __future__ import annotations

from core.pruning import delete_in_batches


def prune_handled_webhooks(cutoff, *, dry_run: bool = False) -> dict:
    from .constants import WebhookStatus
    from .models import WebhookEvent

    rows = WebhookEvent._base_manager.filter(
        created_at__lt=cutoff,
        status__in=[WebhookStatus.PROCESSED, WebhookStatus.IGNORED],
    )
    return {"webhooks": delete_in_batches(rows, dry_run=dry_run)}
