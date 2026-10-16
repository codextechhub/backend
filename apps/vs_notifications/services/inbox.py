"""What a person's in-app inbox holds, for every surface that reports it."""
from __future__ import annotations

from ..constants import ChannelChoices
from ..models import Notification


def unread_in_app_count(user) -> int:
    """The bell badge number for *user*.

    Shared by the count endpoint, by every response that reports the badge and
    by any home screen that shows it, so no two surfaces can describe the same
    inbox differently.
    """
    return Notification.objects.filter(
        recipient=user,
        channel=ChannelChoices.IN_APP,
        is_read=False,
    ).count()
