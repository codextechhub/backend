"""Support tickets past their period, for :mod:`core.pruning`.

A ticket closed 2 years ago goes with its comments, attachments (whose stored
bytes are emptied by the file signals), subscriptions and its own change log.
A ticket only resolved, never closed, is still open to a reply and is kept.
"""
from __future__ import annotations

from core.pruning import delete_in_batches


def prune_closed_tickets(cutoff, *, dry_run: bool = False) -> dict:
    from .constants import TicketStatus
    from .models import Ticket

    rows = Ticket._base_manager.filter(status=TicketStatus.CLOSED, closed_at__lt=cutoff)
    return {"tickets": delete_in_batches(rows, dry_run=dry_run)}
