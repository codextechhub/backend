"""Web Push: the devices a person allowed notifications on, and sending to them.

A push is the ``push`` channel of an ordinary notification
(:mod:`.dispatch`): the event's push template is rendered into the record's
subject and body like any other channel, and :func:`deliver` sends it to
every device the recipient subscribed, through the browser's push service.
A recipient with no device subscribed gets no push record at all.

**What a push says is what can show on a locked screen.** A push template
names what happened ("Your Second Term result is ready", "Payment
confirmed") and never a figure: no mark, no position, no amount. Opening the
app shows the detail to whoever is signed in.

**A shared device.** A subscription made from a device session
(:mod:`vs_user.services.device_sessions`) carries that session's key. Two
children on one parent's phone are two subscriptions; each push starts with
the recipient's first name so the family can tell whose it is, and carries that
child's ``account`` so tapping it opens the app as them. Signing one child
out deletes that child's subscription only (:func:`forget_device`).

A push service that answers 404 or 410 has forgotten the device, so its
subscription is deleted. Keys come from ``WEB_PUSH_VAPID_*`` settings; with
none, push is off: nobody can subscribe and nothing is sent.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("vs_notifications.push")

#: How long a push service keeps a message for a device that is offline.
TIME_TO_LIVE = 24 * 60 * 60


def configured() -> bool:
    return bool(settings.WEB_PUSH_VAPID_PUBLIC_KEY and settings.WEB_PUSH_VAPID_PRIVATE_KEY)


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def subscribe(user, *, endpoint, p256dh, auth, device_key="", user_agent=""):
    """Remember *user*'s device, or refresh the keys of one already known."""
    from ..models import PushSubscription

    row, _created = PushSubscription.all_objects.update_or_create(
        user=user, endpoint_hash=endpoint_hash(endpoint),
        defaults={
            "tenant": user.tenant, "endpoint": endpoint, "p256dh": p256dh, "auth": auth,
            "device_key": device_key or "", "user_agent": (user_agent or "")[:255],
        },
    )
    return row


def unsubscribe(user, endpoint) -> int:
    """Forget *user*'s device; returns how many subscriptions went."""
    from ..models import PushSubscription

    deleted, _ = PushSubscription.all_objects.filter(
        user=user, endpoint_hash=endpoint_hash(endpoint),
    ).delete()
    return deleted


def forget_device(device_key: str) -> int:
    """Delete the subscriptions made under one device session, when it ends."""
    from ..models import PushSubscription

    if not device_key:
        return 0
    deleted, _ = PushSubscription.all_objects.filter(device_key=device_key).delete()
    return deleted


def subscribed_user_ids(user_ids) -> set:
    """Which of *user_ids* have at least one device to push to."""
    from ..models import PushSubscription

    if not configured():
        return set()
    return set(
        PushSubscription.all_objects.filter(
            user_id__in=list(user_ids), user__is_active=True,
        ).values_list("user_id", flat=True).distinct()
    )


def _app_path(user) -> str:
    """Where tapping the push opens: the recipient's own app, else the root."""
    from vs_user.services.device_sessions import device_session_policy

    policy = device_session_policy(user)
    return policy.app_path if policy else "/"


def deliver(notification) -> bool:
    """Send *notification* to each of its recipient's devices; True when any took it."""
    from pywebpush import WebPushException, webpush

    from ..models import PushSubscription

    user = notification.recipient
    if user is None or not configured():
        return False
    subscriptions = list(PushSubscription.all_objects.filter(user=user))
    delivered = False
    for subscription in subscriptions:
        title = notification.subject
        if subscription.device_key and user.first_name:
            title = f"{user.first_name}: {title}"
        message = {
            "title": title,
            "body": notification.body,
            "tag": notification.event_type.key,
            "url": _app_path(user),
            "notification_id": str(notification.pk),
        }
        if subscription.device_key:
            # Which person on a shared device it is for, so a tap opens theirs.
            message["account"] = subscription.device_key
        payload = json.dumps(message)
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=settings.WEB_PUSH_VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.WEB_PUSH_CONTACT},
                ttl=TIME_TO_LIVE,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                subscription.delete()
                continue
            logger.warning("Push to subscription %s failed: %s", subscription.pk, exc)
            continue
        except Exception:  # noqa: BLE001 - one device never stops the others
            logger.exception("Push to subscription %s failed.", subscription.pk)
            continue
        delivered = True
        PushSubscription.all_objects.filter(pk=subscription.pk).update(last_sent_at=timezone.now())
    return delivered
