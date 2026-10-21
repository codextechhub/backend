"""Conversations between the people of one school, who is in them, and what was said.

A conversation is one of six kinds (``ConversationKind``) and every kind has a
fixed shape, which the check constraint holds the database to: a staff direct
conversation names two staff, a family conversation names a guardian, a child
and one member of staff, a pupil conversation names a child and one member of
staff, a group and a broadcast carry a title and an optional branch, and a
broadcast reply names the broadcast it answers.

Who may open a conversation is never stored here. A ``Participant`` row is a
membership period and the place a person's read watermark lives; whether the
person may still read the conversation is derived on every request by
``services.visibility`` from the links that matter at that moment (employment,
the guardian's link to the child, the child being on the roll), so removing the
link ends the access without anybody having to tidy a participant row.

Starting a conversation is find-or-create on ``pair_key``, which is unique per
school when set, so two simultaneous taps make one conversation. Nothing here
is deleted by any route.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import ConversationKind, ParticipantRole, ReplyMode


class _Owned(models.Model):
    """Tenant-enforcing managers, as every school module has them."""

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


def _shape(kind, *, pair: bool, student: bool = False, guardian: bool = False,
           broadcast: bool = False, titled: bool = False, reply_mode: bool = False) -> Q:
    """The columns a conversation of *kind* carries, and the ones it must leave empty."""
    q = Q(kind=kind)
    q &= Q(student__isnull=not student)
    q &= Q(guardian__isnull=not guardian)
    q &= Q(broadcast__isnull=not broadcast)
    q &= ~Q(pair_key="") if pair else Q(pair_key="")
    q &= ~Q(title="") if titled else Q(title="")
    if reply_mode:
        q &= Q(reply_mode__in=ReplyMode.values)
    else:
        q &= Q(reply_mode="")
    if not titled:
        q &= Q(branch__isnull=True)
    return q


class Conversation(_Owned):
    """One conversation: a pair, a group or a broadcast.

    ``branch`` is set only on a group or a broadcast (null is school-wide);
    a family or pupil conversation takes the child's current branch at read
    time and stores none. ``last_message`` and ``last_message_at`` are kept in
    step with each send inside the same transaction, so an inbox row needs no
    query of its own to show its preview.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="conversations",
    )
    kind = models.CharField(max_length=16, choices=ConversationKind.choices)
    #: A group's name or a broadcast's title; blank for every other kind.
    title = models.CharField(max_length=150, blank=True, default="")
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+",
    )
    guardian = models.ForeignKey(
        "vs_students.Guardian", on_delete=models.PROTECT, null=True, blank=True,
        related_name="+",
    )
    #: The broadcast a ``BROADCAST_REPLY`` answers.
    broadcast = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replies",
    )
    reply_mode = models.CharField(
        max_length=16, choices=ReplyMode.choices, blank=True, default="",
    )
    #: ``staff:<low>:<high>``, ``family:<guardian>:<staff>:<student>``,
    #: ``pupil:<student>:<staff>`` or ``reply:<broadcast>:<recipient>``; blank
    #: for groups and broadcasts.
    pair_key = models.CharField(max_length=120, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_message = models.ForeignKey(
        "vs_messages.Message", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        ordering = ["-last_message_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "pair_key"], condition=~Q(pair_key=""),
                name="uq_conversation_pair_key",
            ),
            models.CheckConstraint(
                name="ck_conversation_shape",
                condition=(
                    _shape(ConversationKind.STAFF_DIRECT, pair=True)
                    | _shape(ConversationKind.FAMILY, pair=True, student=True, guardian=True)
                    | _shape(ConversationKind.PUPIL, pair=True, student=True)
                    | _shape(ConversationKind.GROUP, pair=False, titled=True)
                    | _shape(ConversationKind.BROADCAST, pair=False, titled=True,
                             reply_mode=True)
                    | _shape(ConversationKind.BROADCAST_REPLY, pair=True, broadcast=True)
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "kind"]),
            models.Index(fields=["tenant", "student"]),
        ]

    def __str__(self):
        return self.title or f"{self.kind} {self.pk}"


class Participant(_Owned):
    """One membership period of one account in one conversation.

    Leaving or being removed closes the period (``left_at``) and ends access;
    being added again opens a new row, so a person back in a group sees only
    what is said after they returned. ``after_message_id`` is the newest
    message that existed when the period began: the participant reads messages
    with a greater id. ``last_read_message_id`` is the read watermark and only
    ever moves forward.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="participants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+",
    )
    role = models.CharField(max_length=10, choices=ParticipantRole.choices)
    joined_at = models.DateTimeField(default=timezone.now)
    after_message_id = models.BigIntegerField(default=0)
    left_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    last_read_message_id = models.BigIntegerField(default=0)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "user"], condition=Q(left_at__isnull=True),
                name="uq_participant_one_open_period",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "left_at"]),
            models.Index(fields=["conversation", "left_at"]),
        ]


class Message(_Owned):
    """One message. Text only, never edited and never deleted.

    ``client_token`` is minted by the sender's device; a retried send with the
    same token finds the stored message instead of making a second one.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    client_token = models.UUIDField(null=True, blank=True)

    class Meta(_Owned.Meta):
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sender", "client_token"],
                condition=Q(client_token__isnull=False),
                name="uq_message_client_token",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "id"]),
            # Finds the group or broadcast a retried create already made.
            models.Index(
                fields=["sender", "client_token"], condition=Q(client_token__isnull=False),
                name="ix_message_sender_token",
            ),
        ]
