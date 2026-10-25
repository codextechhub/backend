"""Push: the devices a person allowed notifications on, and sending to them.

A push is the ``push`` channel of an ordinary notification
(:mod:`.dispatch`): the event's push template is rendered into the record's
subject and body like any other channel, and :func:`deliver` sends it to
every device the recipient allowed. A recipient with no device gets no push
record at all.

**Two kinds of device, one message.** A browser or installed web app
subscribes through Web Push (``PushSubscription``) and is reached through the
browser's push service. A phone running a store app registers a Firebase
token (``NativePushDevice``, :mod:`.native_push`) and is reached through
Firebase Cloud Messaging (:mod:`.fcm`), which serves Android and iPhone
alike. Both are sent the same message, built once by :func:`build_message`,
so the two can never say different things. Each kind is on only where its
sender is configured: ``WEB_PUSH_VAPID_*`` for browsers
(:func:`configured`) and ``FCM_SERVICE_ACCOUNT_FILE`` for phones
(:func:`phones_configured`); :func:`any_configured` asks whether a person can
be pushed to at all. With neither, nobody can register and nothing is sent.

**What a push says is what can show on a locked screen.** A push template
names what happened ("Your Second Term result is ready", "Payment
confirmed") and never a figure: no mark, no position, no amount. Opening the
app shows the detail to whoever is signed in.

**A shared device.** A device made from a device session
(:mod:`vs_user.services.device_sessions`) carries that session's key. Two
children on one parent's phone are two devices; each push starts with the
recipient's first name so the family can tell whose it is, and carries that
child's ``account`` so tapping it opens the app as them. Signing one child
out deletes that child's devices of both kinds only (:func:`forget_device`).

**Where a tap opens.** By default, the recipient's own app (a pupil's device
session names it) or the root. A module that serves another app registers a
destination (:func:`register_destination`) that may name, for a notification
it recognises, the path to open and the child it is about; the parent portal
does, so a parent's push opens the notice, the child's results or Fees in the
parent app. A destination that fails is logged and skipped; the push still goes.

**A cap on devices.** An account keeps at most four phones and at most four
browsers, counted separately (:data:`MAX_DEVICES_PER_ACCOUNT`). Registering
beyond that drops the device of that kind seen least recently, and the one
just registered is always kept, so a push never fans out to an unbounded
number of senders. Only the registering account's own devices are counted or
touched: two children on one phone each have their own four.

A push service that says it has forgotten a device (HTTP 404 or 410 from a
browser's service; 404 or ``UNREGISTERED`` from Firebase) has that device
deleted. One device failing never stops the others.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from . import fcm

logger = logging.getLogger("vs_notifications.push")

#: How long a push service keeps a message for a device that is offline.
TIME_TO_LIVE = 24 * 60 * 60

#: Phones, and browsers, one account keeps; each kind is counted on its own.
MAX_DEVICES_PER_ACCOUNT = 4


def configured() -> bool:
    """True when this environment sends Web Push to browsers."""
    return bool(settings.WEB_PUSH_VAPID_PUBLIC_KEY and settings.WEB_PUSH_VAPID_PRIVATE_KEY)


def phones_configured() -> bool:
    """True when this environment sends push to phones, through Firebase."""
    return fcm.configured()


def any_configured() -> bool:
    """True when either kind of device can be pushed to."""
    return configured() or phones_configured()


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def keep_newest(model, user, keep_pk) -> int:
    """Delete *user*'s devices of *model* beyond the newest ``MAX_DEVICES_PER_ACCOUNT``.

    Devices are ranked by ``last_seen_at``, then by id, and *keep_pk* (the one
    just registered) is never deleted. Only *user*'s own rows are looked at,
    so another account's devices on the same phone are untouched. There is no
    locking: two registrations racing may briefly leave one device over the
    cap, and the next registration trims it. Returns how many went.
    """
    ranked = model.all_objects.filter(user=user).order_by("-last_seen_at", "-pk")
    surplus = list(ranked.values_list("pk", flat=True)[MAX_DEVICES_PER_ACCOUNT:])
    surplus = [pk for pk in surplus if pk != keep_pk]
    if not surplus:
        return 0
    deleted, _ = model.all_objects.filter(pk__in=surplus).delete()
    return deleted


def subscribe(user, *, endpoint, p256dh, auth, device_key="", user_agent=""):
    """Remember *user*'s browser, or refresh the keys of one already known.

    Subscribing again refreshes ``last_seen_at``, and one browser past the cap
    drops the one seen least recently.
    """
    from ..models import PushSubscription

    row, _created = PushSubscription.all_objects.update_or_create(
        user=user, endpoint_hash=endpoint_hash(endpoint),
        defaults={
            "tenant": user.tenant, "endpoint": endpoint, "p256dh": p256dh, "auth": auth,
            "device_key": device_key or "", "user_agent": (user_agent or "")[:255],
            "last_seen_at": timezone.now(),
        },
    )
    keep_newest(PushSubscription, user, row.pk)
    return row


def unsubscribe(user, endpoint) -> int:
    """Forget *user*'s device; returns how many subscriptions went."""
    from ..models import PushSubscription

    deleted, _ = PushSubscription.all_objects.filter(
        user=user, endpoint_hash=endpoint_hash(endpoint),
    ).delete()
    return deleted


def forget_device(device_key: str) -> int:
    """Delete the browser and phone devices made under one device session, when it ends."""
    from ..models import NativePushDevice, PushSubscription

    if not device_key:
        return 0
    removed = 0
    for model in (PushSubscription, NativePushDevice):
        deleted, _ = model.all_objects.filter(device_key=device_key).delete()
        removed += deleted
    return removed


def subscribed_user_ids(user_ids) -> set:
    """Which of *user_ids* have at least one device to push to."""
    from ..models import NativePushDevice, PushSubscription

    reachable: set = set()
    if not any_configured():
        return reachable
    user_ids = list(user_ids)
    if configured():
        reachable.update(
            PushSubscription.all_objects.filter(
                user_id__in=user_ids, user__is_active=True,
            ).values_list("user_id", flat=True).distinct()
        )
    if phones_configured():
        reachable.update(
            NativePushDevice.all_objects.filter(
                user_id__in=user_ids, user__is_active=True,
            ).values_list("user_id", flat=True).distinct()
        )
    return reachable


@dataclass(frozen=True)
class PushDestination:
    """Where a tap on one push opens (a path on the school's host), and the child it is about."""

    url: str
    child: int | None = None


#: Callables taking a notification and answering a PushDestination, or None to pass.
_DESTINATIONS: list = []


def register_destination(resolver) -> None:
    """Let a module say where the pushes it recognises open; registering twice is one."""
    if resolver not in _DESTINATIONS:
        _DESTINATIONS.append(resolver)


def _destination(notification) -> PushDestination | None:
    for resolver in _DESTINATIONS:
        try:
            found = resolver(notification)
        except Exception:  # noqa: BLE001 - a destination never stops the push
            logger.exception("Push destination for notification %s failed.", notification.pk)
            continue
        if found is not None:
            return found
    return None


def _app_path(user) -> str:
    """Where tapping the push opens: the recipient's own app, else the root."""
    from vs_user.services.device_sessions import device_session_policy

    policy = device_session_policy(user)
    return policy.app_path if policy else "/"


def build_message(notification, *, url, child=None, device_key="") -> dict:
    """The message every kind of device is sent for *notification*.

    The title starts with the recipient's first name on a shared device (one
    made from a device session), and ``account`` names that session so a tap
    opens the app as that person.
    """
    user = notification.recipient
    title = notification.subject
    if device_key and user.first_name:
        title = f"{user.first_name}: {title}"
    message = {
        "title": title,
        "body": notification.body,
        "tag": notification.event_type.key,
        "url": url,
        "notification_id": str(notification.pk),
    }
    if child is not None:
        message["child"] = child
    if device_key:
        message["account"] = device_key
    return message


def deliver(notification) -> bool:
    """Send *notification* to each of its recipient's devices; True when any took it."""
    from ..models import NativePushDevice, PushSubscription

    user = notification.recipient
    if user is None or not any_configured():
        return False
    subscriptions = list(PushSubscription.all_objects.filter(user=user)) if configured() else []
    phones = list(NativePushDevice.all_objects.filter(user=user)) if phones_configured() else []
    if not subscriptions and not phones:
        return False
    destination = _destination(notification)
    url = destination.url if destination is not None else _app_path(user)
    child = destination.child if destination is not None else None
    delivered = False
    for subscription in subscriptions:
        message = build_message(notification, url=url, child=child, device_key=subscription.device_key)
        if _send_to_browser(subscription, message):
            delivered = True
    for phone in phones:
        message = build_message(notification, url=url, child=child, device_key=phone.device_key)
        if _send_to_phone(phone, message):
            delivered = True
    return delivered


def _send_to_browser(subscription, message) -> bool:
    from pywebpush import WebPushException, webpush

    from ..models import PushSubscription

    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(message),
            vapid_private_key=settings.WEB_PUSH_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.WEB_PUSH_CONTACT},
            ttl=TIME_TO_LIVE,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            subscription.delete()
        else:
            logger.warning("Push to subscription %s failed: %s", subscription.pk, exc)
        return False
    except Exception:  # noqa: BLE001 - one device never stops the others
        logger.exception("Push to subscription %s failed.", subscription.pk)
        return False
    PushSubscription.all_objects.filter(pk=subscription.pk).update(last_sent_at=timezone.now())
    return True


def _send_to_phone(phone, message) -> bool:
    from ..models import NativePushDevice

    try:
        outcome = fcm.send(
            phone.token, title=message["title"], body=message["body"],
            data={key: value for key, value in message.items() if key not in ("title", "body")},
            tag=message["tag"], ttl_seconds=TIME_TO_LIVE,
        )
    except Exception:  # noqa: BLE001 - one device never stops the others
        logger.exception("Push to phone device %s failed.", phone.pk)
        return False
    if outcome is fcm.Outcome.FORGOTTEN:
        phone.delete()
        return False
    if outcome is not fcm.Outcome.DELIVERED:
        return False
    NativePushDevice.all_objects.filter(pk=phone.pk).update(last_sent_at=timezone.now())
    return True
