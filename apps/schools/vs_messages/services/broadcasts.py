"""Broadcasts: one message from one member of staff to many, and the private replies.

A broadcast goes to staff only. Its sender chooses, when sending, whether it is
``ONE_WAY`` or takes ``PRIVATE_REPLIES``; the setting never changes afterwards.
Recipients cannot write in the broadcast itself. A reply goes to the sender
alone, in a conversation of its own (kind ``BROADCAST_REPLY``, keyed on the
broadcast and the recipient), which no other recipient and no inspector can
open, so a second reply from the same recipient lands in the same thread.

A recipient reads how many people the broadcast went to, never who; the sender
reads the list.
"""
from __future__ import annotations

from django.db import transaction

from vs_audit.models import AuditActionType

from ..constants import (
    SURFACE_STAFF,
    ConversationKind,
    ParticipantRole,
    ReplyMode,
)
from ..exceptions import (
    BroadcastOneWay,
    ConversationNotFound,
    ConversationReadOnly,
    RecipientUnavailable,
)
from ..models import Conversation, Participant
from . import audit, conversations, groups, messages, people, visibility


@transaction.atomic
def send_broadcast(tenant, actor, *, title, body, recipient_ids, branch_id=None,
                   reply_mode=ReplyMode.ONE_WAY, client_token=None):
    """Send a broadcast; returns ``(conversation, created)``.

    A retry carrying the token of a broadcast already sent returns it and
    sends nothing. Every recipient must be staff the sender may address in the
    broadcast's branch, and never the sender.
    """
    if not people.is_current_staff(tenant, actor):
        raise RecipientUnavailable()
    found = groups.existing_by_token(tenant, actor, client_token, ConversationKind.BROADCAST)
    if found is not None:
        return found, False
    title = groups.clean_title(title, what="broadcast")
    messages.clean_body(body)
    if reply_mode not in ReplyMode.values:
        reply_mode = ReplyMode.ONE_WAY
    branch = groups.branch_for(tenant, actor, branch_id)
    requested = [i for i in groups.clean_ids(recipient_ids, allow_empty=False) if i != actor.pk]
    if not requested:
        raise RecipientUnavailable()
    allowed = people.contactable_staff_ids(
        tenant, actor, requested, narrow_branch_id=branch.pk if branch else None,
    )
    if set(requested) - allowed:
        raise RecipientUnavailable()
    broadcast = Conversation.objects.create(
        tenant=tenant, kind=ConversationKind.BROADCAST, title=title, branch=branch,
        reply_mode=reply_mode, created_by=actor,
    )
    Participant.objects.bulk_create(
        [Participant(tenant=tenant, conversation=broadcast, user=actor, role=ParticipantRole.SENDER)]
        + [
            Participant(tenant=tenant, conversation=broadcast, user_id=i,
                        role=ParticipantRole.RECIPIENT)
            for i in requested
        ]
    )
    audit.record(
        broadcast, AuditActionType.MESSAGE_BROADCAST_SENT, actor,
        f'{actor.full_name} sent the broadcast "{title}" to {len(requested)} people.',
        label=title, metadata={"recipients": len(requested), "reply_mode": reply_mode},
    )
    messages.post_message(broadcast, actor, body, client_token=client_token)
    return broadcast, True


@transaction.atomic
def reply_privately(tenant, user, broadcast_id, *, body, client_token=None):
    """A recipient answers a broadcast's sender, privately.

    Returns ``(reply_conversation, message, created)``. Refused with
    :class:`BroadcastOneWay` when the broadcast takes no replies, and with a
    read-only refusal for the sender, who is not a recipient.
    """
    access = visibility.resolve(tenant, user, broadcast_id, surface=SURFACE_STAFF)
    broadcast = access.conversation
    if broadcast.kind != ConversationKind.BROADCAST:
        raise ConversationNotFound()
    if access.participant is None or access.participant.role != ParticipantRole.RECIPIENT:
        raise ConversationReadOnly("You sent this broadcast, so there is no one to reply to.")
    if broadcast.reply_mode != ReplyMode.PRIVATE_REPLIES:
        raise BroadcastOneWay()
    sender_id = broadcast.created_by_id
    thread, created = conversations.open_conversation(
        tenant, kind=ConversationKind.BROADCAST_REPLY,
        pair_key=f"reply:{broadcast.pk}:{user.pk}", creator=user,
        participants=[(sender_id, ParticipantRole.SENDER), (user.pk, ParticipantRole.RECIPIENT)],
        broadcast=broadcast,
    )
    message, stored = messages.post_message(thread, user, body, client_token=client_token)
    return thread, message, stored
