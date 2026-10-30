"""Conversations past their period, for :mod:`core.pruning`.

A conversation never closes, so it ends when it goes quiet: 3 years after its
last message (or after it was opened, if nobody wrote) it goes with its
messages and participants.

A broadcast's replies hold on to the broadcast, so replies go first, and a
quiet broadcast stays while any reply to it is still in use.
"""
from __future__ import annotations

from django.db.models import Q

from core.pruning import delete_in_batches


def prune_quiet_conversations(cutoff, *, dry_run: bool = False) -> dict:
    from .constants import ConversationKind
    from .models import Conversation

    quiet = Conversation._base_manager.filter(
        Q(last_message_at__lt=cutoff)
        | Q(last_message_at__isnull=True, created_at__lt=cutoff),
    )
    removed = delete_in_batches(
        quiet.exclude(kind=ConversationKind.BROADCAST), dry_run=dry_run,
    )
    # Any reply outside ``quiet`` keeps its broadcast (on a dry run the quiet
    # replies are still here, and are counted as gone).
    in_use = (
        Conversation._base_manager.filter(broadcast__isnull=False)
        .exclude(pk__in=quiet.values("pk")).values("broadcast_id")
    )
    broadcasts = quiet.filter(kind=ConversationKind.BROADCAST).exclude(pk__in=in_use)
    removed += delete_in_batches(broadcasts, dry_run=dry_run)
    return {"conversations": removed}
