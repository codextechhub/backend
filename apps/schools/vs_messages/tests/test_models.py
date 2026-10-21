"""The database holds conversations to their shape, whatever the services do."""
from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from schools.vs_messages.models import Conversation, Message, Participant

from .base import MessagesFixture


class ShapeTests(MessagesFixture):
    """The check and unique constraints on a conversation."""
    def make(self, **fields):
        fields.setdefault("tenant", self.tenant)
        with transaction.atomic():
            return Conversation.all_objects.create(**fields)

    def refused(self, **fields):
        with self.assertRaises(IntegrityError):
            self.make(**fields)

    def test_each_kind_with_its_columns_is_stored(self):
        self.make(kind="STAFF_DIRECT", pair_key="staff:1:2")
        self.make(kind="FAMILY", pair_key="family:1:2:3", student=self.tunde, guardian=self.mother)
        self.make(kind="PUPIL", pair_key="pupil:1:2", student=self.tunde)
        group = self.make(kind="GROUP", title="Maths", branch=self.lekki)
        self.make(kind="BROADCAST", title="Notice", reply_mode="ONE_WAY")
        self.make(kind="BROADCAST_REPLY", pair_key="reply:1:2", broadcast=group)

    def test_a_pair_needs_a_key_and_a_group_must_have_none(self):
        self.refused(kind="STAFF_DIRECT", pair_key="")
        self.refused(kind="GROUP", title="Maths", pair_key="staff:1:2")

    def test_a_child_belongs_to_family_and_pupil_conversations_only(self):
        self.refused(kind="STAFF_DIRECT", pair_key="staff:1:2", student=self.tunde)
        self.refused(kind="FAMILY", pair_key="family:1:2:3", student=self.tunde)
        self.refused(kind="PUPIL", pair_key="pupil:1:2", student=self.tunde, guardian=self.mother)
        self.refused(kind="PUPIL", pair_key="pupil:1:2")

    def test_a_group_and_a_broadcast_are_titled_and_a_pair_is_not(self):
        self.refused(kind="GROUP", title="")
        self.refused(kind="STAFF_DIRECT", pair_key="staff:1:2", title="Chat")

    def test_a_broadcast_has_a_reply_mode_and_nothing_else_does(self):
        self.refused(kind="BROADCAST", title="Notice", reply_mode="")
        self.refused(kind="GROUP", title="Maths", reply_mode="ONE_WAY")

    def test_only_a_group_or_broadcast_carries_a_branch(self):
        self.refused(kind="STAFF_DIRECT", pair_key="staff:1:2", branch=self.lekki)
        self.refused(kind="PUPIL", pair_key="pupil:1:2", student=self.tunde, branch=self.lekki)

    def test_a_reply_names_its_broadcast_and_nothing_else_does(self):
        self.refused(kind="BROADCAST_REPLY", pair_key="reply:1:2")
        group = self.make(kind="GROUP", title="Maths")
        self.refused(kind="GROUP", title="Other", broadcast=group)

    def test_a_pair_key_is_unique_within_a_school_and_free_in_another(self):
        self.make(kind="STAFF_DIRECT", pair_key="staff:1:2")
        self.refused(kind="STAFF_DIRECT", pair_key="staff:1:2")
        Conversation.all_objects.create(tenant=self.solo.tenant, kind="STAFF_DIRECT", pair_key="staff:1:2")

    def test_two_groups_may_share_a_name(self):
        self.make(kind="GROUP", title="Maths")
        self.make(kind="GROUP", title="Maths")


class MembershipTests(MessagesFixture):
    """The constraints on participants and message tokens."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.convo = cls.direct(cls.funke, cls.ibrahim)

    def test_one_open_period_per_person(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Participant.objects.create(
                tenant=self.tenant, conversation=self.convo, user=self.funke, role="MEMBER",
            )

    def test_a_closed_period_does_not_block_a_new_one(self):
        Participant.objects.filter(conversation=self.convo, user=self.funke).update(left_at=timezone.now())
        Participant.objects.create(
            tenant=self.tenant, conversation=self.convo, user=self.funke, role="MEMBER",
        )

    def test_a_token_is_unique_per_sender_and_conversation(self):
        token = uuid.uuid4()
        Message.objects.create(
            tenant=self.tenant, conversation=self.convo, sender=self.funke, body="a", client_token=token,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Message.objects.create(
                tenant=self.tenant, conversation=self.convo, sender=self.funke, body="b",
                client_token=token,
            )

    def test_messages_without_a_token_never_collide(self):
        for text in ("a", "b"):
            Message.objects.create(
                tenant=self.tenant, conversation=self.convo, sender=self.funke, body=text,
            )
