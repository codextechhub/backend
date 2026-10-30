"""Notifications past their period, for :mod:`core.pruning`.

One row is one notification on one channel: an in-app line, or an email with
its whole text. A year after it was made it has been read or never will be.
An email still waiting to go is kept whatever its age: a stuck send is a fault
to see, not history to tidy.
"""
from __future__ import annotations

from core.pruning import delete_in_batches


def prune_notifications(cutoff, *, dry_run: bool = False) -> dict:
    from .constants import NotificationStatus
    from .models import Notification

    rows = Notification._base_manager.filter(created_at__lt=cutoff).exclude(
        status=NotificationStatus.PENDING,
    )
    return {"notifications": delete_in_batches(rows, dry_run=dry_run)}
