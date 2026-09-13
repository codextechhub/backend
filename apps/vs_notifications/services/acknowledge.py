"""Clearing a notification by reading the record it announced.

Acknowledgement by navigation only ever covers one client. The frontend reports
the path it opened, the API clears whatever pointed there, and every other way
of reading a record goes unaccounted for: a requisition read in a side drawer, a
ticket opened in a modal, a run inspected by a script or a second client. None
of those change a route, so none of them clear anything, and the bell keeps
offering a notice whose record the reader has already read.

Reading the record IS the acknowledgement. :func:`acknowledge_record` runs on
the record's own read endpoint, so the clear happens however the record was
reached, and the navigation call becomes a second route to the same outcome
rather than the only one.

The rules come from ``routing.RECORD_DESTINATIONS``, the same table that builds
the links, so a family cannot have a read rule without a destination or a
destination without a read rule.

Scope is the requesting user's own unread in-app rows and nothing wider: one
person reading a record says nothing about whether the other recipients of the
same notice have read theirs.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from ..constants import ChannelChoices
from ..models import Notification
from .routing import family_filter_q

logger = logging.getLogger(__name__)


def acknowledge_record(user, *, family, value) -> int:
    """Mark the caller's unread notifications about one record as read.

    Returns the number of rows cleared, and never raises. This runs on a read
    path, where failing to clear a bell entry is a far smaller loss than
    failing the read the caller actually asked for, so a broken rule is logged
    and the read proceeds. The atomic block is a savepoint when a caller is
    already in a transaction, which keeps a swallowed database error from
    poisoning the surrounding work.

    One UPDATE, no pre-check. The (recipient, channel, is_read, -created_at)
    index reduces the candidate set to a single user's unread rows, so the
    metadata comparison runs over the handful that survive it.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    if value in (None, ""):
        return 0
    try:
        with transaction.atomic():
            return Notification.all_objects.filter(
                family_filter_q(family, value),
                recipient=user,
                channel=ChannelChoices.IN_APP,
                is_read=False,
            ).update(is_read=True, read_at=timezone.now())
    except Exception:
        logger.warning(
            "Could not acknowledge %s notifications for record %s.",
            family, value, exc_info=True,
        )
        return 0
