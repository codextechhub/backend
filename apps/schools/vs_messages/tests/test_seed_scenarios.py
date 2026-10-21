"""The development seeder puts every kind of conversation in place, once, and tells nobody.

Brightfield has two branches, parents and pupils with accounts, and every
messaging key; Sunrise has one branch and too few staff for a group, which the
seeder says and skips.
"""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command

from schools.vs_messages.models import Conversation, Message
from vs_notifications.models import Notification

from .base import MessagesFixture


class SeedScenarioTests(MessagesFixture):
    def seed(self, slug="brightfield"):
        out = StringIO()
        with mock.patch("vs_notifications.notify.send_notification") as send:
            call_command("seed_messaging_scenarios", school=slug, like="lagoon-view", stdout=out)
        send.assert_not_called()
        return out.getvalue()

    def test_a_two_branch_school_gets_every_kind_of_conversation(self):
        printed = self.seed()
        kinds = set(Conversation.objects.values_list("kind", flat=True))
        self.assertEqual(
            kinds,
            {"STAFF_DIRECT", "GROUP", "BROADCAST", "BROADCAST_REPLY", "FAMILY", "PUPIL"},
        )
        self.assertEqual(Conversation.objects.filter(kind="BROADCAST").count(), 2)
        self.assertIn("funke.bello@example.ng", printed)
        self.assertIn("group member added later", printed)
        self.assertIn("primary guardian", printed)
        self.assertNotIn("skipped", printed)

    def test_the_late_group_member_cannot_read_what_came_before(self):
        self.seed()
        group = Conversation.objects.get(kind="GROUP")
        late = group.participants.exclude(after_message_id=0).get()
        seen = Message.objects.filter(conversation=group, id__gt=late.after_message_id)
        self.assertEqual(seen.count(), 1)
        self.assertGreater(Message.objects.filter(conversation=group).count(), 1)

    def test_the_leader_takes_part_in_nothing_they_inspect(self):
        self.seed()
        for kind in ("GROUP", "BROADCAST", "PUPIL"):
            with self.subTest(kind=kind):
                conversation = Conversation.objects.filter(kind=kind).first()
                self.assertFalse(
                    conversation.participants.filter(user=self.admin).exists(),
                )

    def test_running_it_again_changes_nothing(self):
        self.seed()
        conversations = Conversation.objects.count()
        said = Message.objects.count()
        self.seed()
        self.assertEqual(Conversation.objects.count(), conversations)
        self.assertEqual(Message.objects.count(), said)

    def test_it_alerts_nobody(self):
        self.seed()
        self.assertFalse(Notification.all_objects.filter(event_type__key__startswith="messages.").exists())

    def test_a_one_branch_school_skips_what_it_cannot_do_and_says_so(self):
        printed = self.seed("sunrise")
        self.assertIn("group: skipped", printed)
        self.assertIn("broadcast: skipped", printed)
        kinds = set(Conversation.objects.filter(tenant=self.solo.tenant).values_list("kind", flat=True))
        self.assertIn("FAMILY", kinds)
        self.assertIn("PUPIL", kinds)
