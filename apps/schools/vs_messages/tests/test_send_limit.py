"""The send limit: 20 messages a minute per person, shared by every route that stores one.

At Brightfield, Mrs. Funke Adeyemi sends twenty messages to Mr. Musa in a
minute and the twenty-first is refused with a sentence she can read and the
seconds to wait; nothing is stored. Her limit is hers alone: Mr. Musa is not
held up by it, and Mrs. Funke Bello (a parent) and Tunde (a pupil) each have a
budget of their own on their portal routes. Reading never counts, because the
inbox polls.

The throttle's rate and clock are patched: the test settings switch every rate
off, and a patched clock moves the minute without sleeping.
"""
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from schools.vs_messages.models import Conversation, Message
from schools.vs_messages.throttles import MessageSendThrottle

from .base import MessagesFixture

SENTENCE = "You are sending messages too quickly. Wait a moment and try again."
LIMIT = 20


class Clock:
    """A callable clock the test moves by hand."""

    def __init__(self, now=10_000.0):
        self.now = now

    def __call__(self):
        return self.now


class SendLimitFixture(MessagesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chat = cls.direct(cls.funke, cls.ibrahim)
        cls.other_chat = cls.direct(cls.funke, cls.ngozi)
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim])
        cls.family = cls.family_chat(cls.funke, cls.tunde, cls.mother)
        cls.pupil_thread = cls.pupil_chat(cls.funke, cls.tunde)

    def setUp(self):
        cache.clear()
        self.clock = Clock()
        for patcher in (
            mock.patch.object(MessageSendThrottle, "THROTTLE_RATES", {"message_send": f"{LIMIT}/minute"}),
            mock.patch.object(MessageSendThrottle, "timer", self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_up(self, user):
        """Record a full minute of sends for *user* without making them."""
        key = MessageSendThrottle.cache_format % {
            "scope": "message_send", "ident": f"{user.tenant_id}:{user.pk}",
        }
        cache.set(key, [self.clock.now] * LIMIT, 60)

    def send(self, user, pk, text="Hello"):
        return self.post(user, "messages-messages", {"body": text}, pk=pk)

    def assertRefused(self, response):
        self.assertEqual(response.status_code, 429, response.data)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["error"]["code"], "MESSAGE_RATE_LIMITED")
        self.assertEqual(response.data["message"], SENTENCE)
        retry = response.data["error"]["detail"]["retry_after"]
        self.assertTrue(1 <= retry <= 60, retry)


class RateTests(SimpleTestCase):
    def test_the_rate_is_twenty_a_minute_in_the_base_settings(self):
        from apps.settings import base

        self.assertEqual(base.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["message_send"], "20/minute")


class StaffLimitTests(SendLimitFixture):
    def test_the_twenty_first_message_in_a_minute_is_refused_and_not_stored(self):
        for n in range(LIMIT):
            self.assertEqual(self.send(self.funke, self.chat.pk, f"Message {n}").status_code, 201)
        stored = Message.objects.count()

        self.assertRefused(self.send(self.funke, self.chat.pk, "One too many"))
        self.assertEqual(Message.objects.count(), stored)
        self.assertFalse(Message.objects.filter(body="One too many").exists())

    def test_the_budget_is_shared_across_conversations_and_routes(self):
        for n in range(LIMIT):
            self.assertEqual(self.send(self.funke, self.chat.pk, f"Message {n}").status_code, 201)
        stored = Message.objects.count()

        self.assertRefused(self.send(self.funke, self.other_chat.pk))
        self.assertRefused(self.post(
            self.funke, "messages-broadcast-reply", {"body": "Ok"}, pk=self.notice.pk,
        ))
        self.assertRefused(self.post(self.funke, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": self.segun.pk, "body": "Hi",
        }))
        self.assertEqual(Message.objects.count(), stored)

    def test_every_staff_route_that_stores_a_message_is_covered(self):
        self.use_up(self.lekki_head)
        before = (Message.objects.count(), Conversation.objects.count())

        responses = [
            self.send(self.lekki_head, self.chat.pk),
            self.post(self.lekki_head, "messages-conversations", {
                "kind": "STAFF_DIRECT", "recipient": self.segun.pk, "body": "Hi",
            }),
            self.post(self.lekki_head, "messages-group-create", {
                "name": "Maths", "branch": self.lekki.pk, "members": [self.funke.pk], "body": "Hi",
            }),
            self.post(self.lekki_head, "messages-broadcast-create", {
                "title": "Notice", "body": "Friday", "branch": self.lekki.pk,
                "recipients": [self.funke.pk], "reply_mode": "ONE_WAY",
            }),
            self.post(self.lekki_head, "messages-broadcast-reply", {"body": "Ok"}, pk=self.notice.pk),
        ]

        for response in responses:
            self.assertRefused(response)
        self.assertEqual((Message.objects.count(), Conversation.objects.count()), before)

    def test_reading_is_never_refused_and_never_uses_budget(self):
        self.use_up(self.funke)

        for _ in range(30):
            self.assertEqual(self.get(self.funke, "messages-conversations").status_code, 200)
            self.assertEqual(self.get(self.funke, "messages-messages", pk=self.chat.pk).status_code, 200)
            self.assertEqual(self.get(self.funke, "messages-unread-count").status_code, 200)
        self.assertEqual(self.post(self.funke, "messages-read", pk=self.chat.pk).status_code, 200)

    def test_polling_leaves_the_whole_budget_to_sending(self):
        for _ in range(30):
            self.get(self.funke, "messages-conversations")
            self.get(self.funke, "messages-unread-count")

        for n in range(LIMIT):
            self.assertEqual(self.send(self.funke, self.chat.pk, f"Message {n}").status_code, 201)
        self.assertRefused(self.send(self.funke, self.chat.pk))

    def test_each_person_has_a_budget_of_their_own(self):
        self.use_up(self.funke)

        self.assertRefused(self.send(self.funke, self.chat.pk))
        self.assertEqual(self.send(self.ibrahim, self.chat.pk, "I am here").status_code, 201)

    def test_sending_works_again_when_the_minute_has_passed(self):
        self.use_up(self.funke)
        self.assertRefused(self.send(self.funke, self.chat.pk))

        self.clock.now += 30
        self.assertRefused(self.send(self.funke, self.chat.pk))
        self.clock.now += 31
        self.assertEqual(self.send(self.funke, self.chat.pk, "Back again").status_code, 201)

    def test_the_wait_shrinks_as_the_minute_passes(self):
        self.use_up(self.funke)
        self.clock.now += 45

        response = self.send(self.funke, self.chat.pk)

        self.assertRefused(response)
        self.assertEqual(response.data["error"]["detail"]["retry_after"], 15)

    def test_a_single_branch_school_is_limited_the_same_way(self):
        chat = self.direct_at_sunrise()
        for n in range(LIMIT):
            self.assertEqual(self.send(self.ada, chat.pk, f"Message {n}").status_code, 201)

        self.assertRefused(self.send(self.ada, chat.pk))
        self.assertEqual(self.send(self.solo_admin, chat.pk, "Reply").status_code, 201)

    def direct_at_sunrise(self):
        from schools.vs_messages.services import conversations

        conversation, _, _ = conversations.start_staff_direct(
            self.solo.tenant, self.ada, recipient_id=self.solo_admin.pk, body="Hello",
        )
        return conversation

    def test_a_caller_refused_for_a_missing_key_is_not_told_to_slow_down(self):
        self.use_up(self.reader)

        response = self.send(self.reader, self.chat.pk)

        self.assertEqual(response.status_code, 403)


class PortalLimitTests(SendLimitFixture):
    def test_a_parent_is_limited_on_the_portal_routes(self):
        self.use_up(self.mother.user)
        before = Message.objects.count()

        self.assertRefused(self.post(
            self.mother.user, "parent-messages-messages", {"body": "Hi"}, pk=self.family.pk,
        ))
        self.assertRefused(self.post(self.mother.user, "parent-messages-conversations", {
            "child": self.tunde.pk, "staff": self.ngozi.pk, "body": "Hi",
        }))
        self.assertEqual(Message.objects.count(), before)
        self.assertEqual(self.get(self.mother.user, "parent-messages-conversations").status_code, 200)
        self.assertEqual(self.get(self.mother.user, "parent-messages-unread-count").status_code, 200)

    def test_a_parents_budget_is_not_the_pupils(self):
        self.use_up(self.mother.user)

        self.assertEqual(self.post(
            self.tunde.user, "student-messages-messages", {"body": "Hi madam"}, pk=self.pupil_thread.pk,
        ).status_code, 201)

    def test_a_pupil_is_limited_on_the_portal_routes(self):
        self.use_up(self.tunde.user)
        before = Message.objects.count()

        self.assertRefused(self.post(
            self.tunde.user, "student-messages-messages", {"body": "Hi"}, pk=self.pupil_thread.pk,
        ))
        self.assertRefused(self.post(self.tunde.user, "student-messages-conversations", {
            "staff": self.ngozi.pk, "body": "Hi",
        }))
        self.assertEqual(Message.objects.count(), before)
        self.assertEqual(self.get(self.tunde.user, "student-messages-conversations").status_code, 200)

    def test_a_parent_sending_twenty_messages_is_stopped_at_the_twenty_first(self):
        for n in range(LIMIT):
            self.assertEqual(self.post(
                self.mother.user, "parent-messages-messages", {"body": f"Note {n}"}, pk=self.family.pk,
            ).status_code, 201)

        self.assertRefused(self.post(
            self.mother.user, "parent-messages-messages", {"body": "Too many"}, pk=self.family.pk,
        ))
