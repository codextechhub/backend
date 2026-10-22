"""Read receipts: whether the people a sender wrote to have read what they wrote.

A receipt belongs to the sender alone and only on the sender's own messages::

    {"state": "sent" | "read", "read_count": n, "recipient_count": n}

**Who counts.** The readers of a message are the other open participants of the
conversation who could see it (their ``after_message_id`` is below the
message) and who are current now by the same rules :mod:`.visibility` judges
access: current staff for a staff role, a guardian of a family conversation
only while the link to the child holds and the child is served by the portal,
a pupil of a pupil conversation only while on the roll with a working account.
Someone who left, who joined after the message, or who is no longer current is
not a reader, so a deactivated colleague can never hold a group's tick back.

**When it is read.** A reader has read a message when their read watermark has
reached it. The state is ``read`` when every reader has read it and there is at
least one reader; a direct conversation has one reader, a group or a broadcast
counts everyone, and ``read_count`` over ``recipient_count`` is what the sender
sees as "Read by 12 of 40".

**Who sees one.** Nobody while the school setting is off, and never an
inspector or a primary guardian watching a pupil conversation: neither holds a
participation, so neither moves a watermark and neither is counted as a reader.

**Cost.** One query per call reads the conversation's open, current
participants with their watermarks; every message is then judged in memory, so
a page of fifty messages in a group of forty costs what a direct conversation
costs.
"""
from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

from ..constants import STAFF_ROLES, ConversationKind, ParticipantRole
from ..models import Participant
from .people import current_staff_user_ids

STATE_SENT = "sent"
STATE_READ = "read"


def applies(access, settings) -> bool:
    """Whether the caller of *access* is shown receipts at all.

    True for a participant while the school setting is on. An inspector and a
    watching primary guardian are not participants and are never shown any.
    """
    return bool(settings.read_receipts) and access.participant is not None and not access.inspecting


def _current_readers(conversation, user):
    """``(after_message_id, last_read_message_id)`` of each current reader, in one query."""
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import Student, StudentGuardian

    tenant_id = conversation.tenant_id
    current = Q(role__in=STAFF_ROLES, user_id__in=current_staff_user_ids(tenant_id))
    if conversation.kind == ConversationKind.FAMILY:
        linked = Exists(StudentGuardian.objects.filter(
            student_id=conversation.student_id, guardian_id=conversation.guardian_id,
            guardian__user_id=OuterRef("user_id"), guardian__user__is_active=True,
        ))
        served = Exists(Student.objects.filter(
            pk=conversation.student_id, status__in=PORTAL_STATUSES,
        ))
        current |= Q(role=ParticipantRole.GUARDIAN) & linked & served
    elif conversation.kind == ConversationKind.PUPIL:
        on_roll = Exists(Student.objects.filter(
            pk=conversation.student_id, user_id=OuterRef("user_id"), user__is_active=True,
            status__in=PORTAL_STATUSES,
        ))
        current |= Q(role=ParticipantRole.PUPIL) & on_roll
    return list(
        Participant.objects.filter(conversation_id=conversation.pk, left_at__isnull=True)
        .exclude(user_id=user.pk)
        .filter(current)
        .values_list("after_message_id", "last_read_message_id")
    )


def for_messages(access, user, messages, *, settings) -> dict:
    """``{message id: receipt}`` for the messages among *messages* that *user* sent.

    Empty when receipts do not apply to the caller (see :func:`applies`) or
    none of the messages is theirs. Messages by other people get no entry.
    """
    if not applies(access, settings):
        return {}
    own = [m for m in messages if m.sender_id == user.pk]
    if not own:
        return {}
    readers = _current_readers(access.conversation, user)
    out = {}
    for message in own:
        seen = [read for after, read in readers if after < message.pk]
        read_count = sum(1 for read in seen if read >= message.pk)
        recipient_count = len(seen)
        done = recipient_count > 0 and read_count == recipient_count
        out[message.pk] = {
            "state": STATE_READ if done else STATE_SENT,
            "read_count": read_count,
            "recipient_count": recipient_count,
        }
    return out


class Receipts:
    """The receipts one caller is shown in one conversation, as a callable.

    ``Receipts(access, user, settings, allowed=...)(messages)`` is
    :func:`for_messages`. ``allowed`` is False where the caller's Field Access
    hides receipts, which closes the lookup before any query. ``enabled`` says
    whether the caller is shown receipts at all, so a route can leave its
    receipt block out instead of serving an empty one.
    """

    def __init__(self, access, user, settings, *, allowed: bool = True):
        self.access = access
        self.user = user
        self.settings = settings
        self.enabled = bool(allowed) and applies(access, settings)

    def __call__(self, messages) -> dict:
        if not self.enabled:
            return {}
        return for_messages(self.access, self.user, messages, settings=self.settings)
