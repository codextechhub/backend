"""Phone devices: the installs of a store app a person allowed notifications on.

A phone registers the token Firebase Cloud Messaging gave the install
(:mod:`.fcm`). A row belongs to one account on one phone: registering the same
token as a second account makes a second row, so two children on one parent's
phone are two devices, each with its own device session key. A row is only
ever created, refreshed or removed by the person it belongs to. An account
keeps at most :data:`.push.MAX_DEVICES_PER_ACCOUNT` phones: registering one
more drops the phone seen least recently (:func:`.push.keep_newest`).
"""
from __future__ import annotations

import hashlib

from django.utils import timezone

from . import push


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register(user, *, token, platform, app_id, app_version="", device_key=""):
    """Remember *user*'s phone, or refresh the row of one already known."""
    from ..models import NativePushDevice

    row, _created = NativePushDevice.all_objects.update_or_create(
        user=user, token_hash=token_hash(token),
        defaults={
            "tenant": user.tenant, "platform": platform, "app_id": app_id, "token": token,
            "app_version": (app_version or "")[:32], "device_key": device_key or "",
            "last_seen_at": timezone.now(),
        },
    )
    push.keep_newest(NativePushDevice, user, row.pk)
    return row


def unregister(user, token) -> int:
    """Forget *user*'s phone; returns how many devices went."""
    from ..models import NativePushDevice

    deleted, _ = NativePushDevice.all_objects.filter(
        user=user, token_hash=token_hash(token),
    ).delete()
    return deleted
