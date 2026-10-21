"""Telling people that a message has reached them.

Saving a message never depends on telling anybody. A send commits first and the
alert follows, so the worker can always find the message it was sent and a send
that rolls back tells nobody. Every failure on the way, from the broker to a
single batch of recipients, is logged and swallowed: a message that was stored
stays stored, and the API answered it already.

**An alert never carries the message.** It says who wrote and where, and the
recipient opens the conversation to read it. Module 8 delivers it on the
channels the recipient has (in-app, email, push) and stores nothing of the
conversation itself.

**One alert per run of messages.** :func:`..services.visibility.alert_recipients`
leaves out anybody who already has something unread in the conversation, so a
burst of ten messages is one alert, and reading the conversation re-arms it.

**Names follow Field Access.** A member of staff who may not read People in the
conversation is told "a colleague" wrote, and one who may not read the Group or
broadcast name is told "a staff group". That is judged per recipient and the
recipients with the same wording are sent one notification between them, so a
group of two hundred is a handful of sends, not two hundred. A parent or a pupil
holds no role and is always told the member of staff's name.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from celery import shared_task
from django.db import transaction

from .constants import (
    EVENT_NEW_BROADCAST,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_MESSAGE_PARENT,
    EVENT_NEW_MESSAGE_PUPIL,
    FIELD_RESOURCE,
    STAFF_ROLES,
    UNNAMED_BROADCAST,
    UNNAMED_COLLEAGUE,
    UNNAMED_GROUP,
    UNNAMED_PARENT,
    UNNAMED_PUPIL,
    ConversationKind,
    ParticipantRole,
)

logger = logging.getLogger(__name__)

#: Recipients per call, so a broadcast to a thousand staff is a few calls
#: rather than one enormous one.
BATCH = 200

_SUPPRESSED: ContextVar[bool] = ContextVar("vs_messages_alerts_suppressed", default=False)


@contextmanager
def alerts_suppressed():
    """Store messages inside this block without alerting anybody.

    For tooling that writes conversations on purpose (a development seeder, an
    import) and must not wake real devices.
    """
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def queue_message_alert(message_id: int) -> None:
    """Hand *message_id* to the alert worker once the sending transaction commits.

    Called by :func:`schools.vs_messages.services.messages.post_message` for
    every stored message, inside the sender's transaction.
    """
    if _SUPPRESSED.get():
        return
    transaction.on_commit(lambda: _enqueue(message_id))


def _enqueue(message_id: int) -> None:
    """Queue the task; a broker that is down loses the alert and nothing else."""
    try:
        dispatch_message_alert.delay(message_id)
    except Exception:
        logger.exception("Message %s: the alert could not be queued.", message_id)


def clear_message_alerts(user, conversation_id: int) -> None:
    """Clear *user*'s bell entries for *conversation_id*, because they have read it.

    Called by :func:`schools.vs_messages.services.messages.mark_read`. Reading
    the conversation is the acknowledgement, whichever client did the reading.
    """
    from vs_notifications.services.acknowledge import acknowledge_record
    from vs_notifications.services.routing import RecordFamily

    acknowledge_record(user, family=RecordFamily.CONVERSATION, value=conversation_id)


@shared_task(name="vs_messages.dispatch_message_alert")
def dispatch_message_alert(message_id: int) -> int:
    """Tell everybody who should hear of *message_id*; returns how many were addressed.

    Never raises: the message is already saved and answered.
    """
    try:
        return _dispatch(message_id)
    except Exception:
        logger.exception("Message %s: the alert failed.", message_id)
        return 0


def _dispatch(message_id: int) -> int:
    from vs_notifications.notify import send_notification

    from .models import Message
    from .services import visibility

    message = (
        Message.all_objects.select_related(
            "sender", "conversation__tenant", "conversation__student", "conversation__guardian",
        )
        .filter(pk=message_id)
        .first()
    )
    if message is None:
        return 0
    conversation = message.conversation
    recipients = visibility.alert_recipients(message)
    if not recipients:
        return 0

    told = 0
    for event_key, context, users in _plans(message, conversation, recipients):
        for start in range(0, len(users), BATCH):
            batch = users[start:start + BATCH]
            try:
                send_notification(
                    event_key=event_key,
                    context=context,
                    recipients=batch,
                    tenant=conversation.tenant,
                    metadata={"conversation_id": conversation.pk},
                )
            except Exception:
                logger.exception(
                    "Message %s: telling %s people (%s) failed.", message.pk, len(batch), event_key,
                )
                continue
            told += len(batch)
    logger.info("Message %s told %s people.", message.pk, told)
    return told


# ── Who hears what ────────────────────────────────────────────────────────


def _plans(message, conversation, recipients):
    """``(event key, context, users)`` for each distinct wording of the alert."""
    tenant = conversation.tenant
    staff = [p.user for p in recipients if p.role in STAFF_ROLES]
    guardians = [p.user for p in recipients if p.role == ParticipantRole.GUARDIAN]
    pupils = [p.user for p in recipients if p.role == ParticipantRole.PUPIL]
    base = {"tenant_name": _school_name(tenant)}

    if staff:
        event_key = _staff_event(message, conversation)
        url = _conversation_url(tenant, "staff", conversation.pk)
        for (sender_label, conversation_label), users in _staff_wording(
            message, conversation, staff, event_key,
        ).items():
            yield event_key, {
                **base, "sender_label": sender_label,
                "conversation_label": conversation_label, "conversation_url": url,
            }, users
    sender_name = _name(message.sender)
    if guardians:
        yield EVENT_NEW_MESSAGE_PARENT, {
            **base, "sender_label": sender_name,
            "child_first_name": conversation.student.first_name,
            "conversation_url": _conversation_url(tenant, "parent", conversation.pk),
        }, guardians
    if pupils:
        yield EVENT_NEW_MESSAGE_PUPIL, {
            **base, "sender_label": sender_name,
            "conversation_url": _conversation_url(tenant, "student", conversation.pk),
        }, pupils


def _staff_event(message, conversation) -> str:
    """A broadcast's first message announces it; everything else is a new message."""
    from .models import Message

    if conversation.kind != ConversationKind.BROADCAST:
        return EVENT_NEW_MESSAGE
    earlier = Message.all_objects.filter(conversation_id=conversation.pk, id__lt=message.pk)
    return EVENT_NEW_MESSAGE if earlier.exists() else EVENT_NEW_BROADCAST


def _staff_wording(message, conversation, users, event_key) -> dict:
    """``{(sender label, conversation label): [users]}`` after each user's Field Access.

    The conversation label is empty unless the alert is about a message in a
    group or in a broadcast already announced; a direct conversation is named
    by its sender alone.
    """
    from vs_rbac.field_evaluator import get_field_access

    names_key = f"{FIELD_RESOURCE}.participants"
    title_key = f"{FIELD_RESOURCE}.title"
    titled = (
        event_key == EVENT_NEW_MESSAGE
        and conversation.kind in (ConversationKind.GROUP, ConversationKind.BROADCAST)
    )
    unnamed_title = UNNAMED_GROUP if conversation.kind == ConversationKind.GROUP else UNNAMED_BROADCAST
    named = _name(message.sender)
    unnamed = _unnamed_sender(message, conversation)

    grouped: dict = {}
    for user in users:
        access = get_field_access(user, conversation.tenant)
        sender_label = named if access.can_read(names_key) else unnamed
        label = ""
        if titled:
            label = conversation.title if access.can_read(title_key) else unnamed_title
        grouped.setdefault((sender_label, label), []).append(user)
    return grouped


def _unnamed_sender(message, conversation) -> str:
    """How a sender is described to somebody who may not read names."""
    sender_id = message.sender_id
    if sender_id is not None:
        if conversation.kind == ConversationKind.FAMILY and conversation.guardian is not None:
            if conversation.guardian.user_id == sender_id:
                return UNNAMED_PARENT
        if conversation.kind == ConversationKind.PUPIL and conversation.student is not None:
            if conversation.student.user_id == sender_id:
                return UNNAMED_PUPIL
    return UNNAMED_COLLEAGUE


def _name(user) -> str:
    return ((user.full_name or user.email) if user is not None else "") or "Someone"


def _school_name(tenant) -> str:
    profile = getattr(tenant, "school_profile", None)
    return getattr(profile, "name", "") or tenant.name


def _conversation_url(tenant, surface: str, conversation_id: int) -> str:
    from .links import conversation_url

    return conversation_url(tenant, surface, conversation_id)
