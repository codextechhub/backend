"""Writing a message, and reading up to it.

:func:`send_message` is the door for a person typing into a conversation they
have open: it refuses when the conversation is read-only for them. The
creation flows (a new group's first message, a broadcast's text) and the
private broadcast reply use :func:`post_message`, which stores the message for
a sender the calling service has already checked.

A send is serialised on the conversation row, so two writers cannot leave
``last_message`` pointing at the older of their messages, and a retried send
carrying the same ``client_token`` finds the stored message and stores nothing.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import F, Max, Value, When, Case
from django.db.models.functions import Greatest
from django.utils import timezone

from ..constants import BODY_MAX
from ..exceptions import BodyRequired, BodyTooLong, ConversationReadOnly
from ..models import Conversation, Message, Participant
from .. import tasks
from . import visibility


def clean_body(body) -> str:
    """The message text trimmed, refused when empty or longer than the limit."""
    text = (body or "").strip()
    if not text:
        raise BodyRequired()
    if len(text) > BODY_MAX:
        raise BodyTooLong(f"Keep a message to {BODY_MAX:,} characters or fewer.")
    return text


@transaction.atomic
def post_message(conversation, sender, body, *, client_token=None):
    """Store *body* as *sender*'s message in *conversation*; ``(message, created)``.

    The sender's own read watermark moves to the new message, so a person never
    has their own words counted as unread. Alerts are queued for after the
    transaction commits.
    """
    text = clean_body(body)
    locked = Conversation.all_objects.select_for_update().get(pk=conversation.pk)
    if client_token:
        stored = Message.objects.filter(
            conversation_id=locked.pk, sender=sender, client_token=client_token,
        ).first()
        if stored is not None:
            return stored, False
    message = Message.objects.create(
        tenant_id=locked.tenant_id, conversation=locked, sender=sender, body=text,
        client_token=client_token or None,
    )
    Conversation.all_objects.filter(pk=locked.pk).update(
        last_message=message, last_message_at=message.created_at,
    )
    Participant.objects.filter(
        conversation_id=locked.pk, user_id=sender.pk, left_at__isnull=True,
    ).update(
        last_read_message_id=Greatest(F("last_read_message_id"), Value(message.pk)),
        last_read_at=message.created_at,
    )
    tasks.queue_message_alert(message.pk)
    return message, True


def send_message(access, *, sender, body, client_token=None):
    """A participant writes in the conversation *access* was resolved for.

    Raises :class:`ConversationReadOnly` carrying the reason when the caller
    may read but not write. Returns ``(message, created)``; ``created`` is
    False when the token matched a message already stored.
    """
    if not access.can_send:
        raise ConversationReadOnly(access.reason or "")
    return post_message(access.conversation, sender, body, client_token=client_token)


def mark_read(access, user, *, up_to=None) -> int:
    """Move the caller's read watermark forward and return what is still unread.

    The watermark only ever moves forward, in the database, so two devices
    reading at once cannot move it back. ``up_to`` is clamped to the newest
    message the caller may read; without it the newest one is meant. An
    inspector or a primary guardian has no watermark and nothing changes.
    """
    participant = access.participant
    if participant is None:
        return 0
    newest = visibility.visible_messages(access).aggregate(top=Max("id"))["top"] or 0
    target = newest if up_to is None else min(int(up_to), newest)
    if target > 0:
        Participant.objects.filter(pk=participant.pk).update(
            last_read_at=Case(
                When(last_read_message_id__lt=target, then=Value(timezone.now())),
                default=F("last_read_at"),
            ),
            last_read_message_id=Greatest(F("last_read_message_id"), Value(target)),
        )
    tasks.clear_message_alerts(user, access.conversation.pk)
    return visibility.conversation_unread(access, user)
