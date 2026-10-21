"""Broadcasts: one message to many staff, and the replies that stay private.

The reply setting is fixed when a broadcast is sent. A reply opens a
conversation between the recipient and the sender only; the other recipients
and every inspector are shut out of it.
"""
from __future__ import annotations

import uuid

from schools.vs_messages.models import Conversation, Participant
from schools.vs_messages.services import visibility

from .base import MessagesFixture, audit_events

MISSING = 999999


class SendTests(MessagesFixture):
    """Sending a broadcast: who it may go to and what it needs."""
    def send(self, user, **body):
        body.setdefault("title", "Staff meeting")
        body.setdefault("body", "Friday at 2pm")
        body.setdefault("branch", self.lekki.pk)
        body.setdefault("reply_mode", "PRIVATE_REPLIES")
        return self.post(user, "messages-broadcast-create", body)

    def test_a_broadcast_goes_to_its_recipients(self):
        response = self.send(self.lekki_head, recipients=[self.funke.pk, self.ibrahim.pk])
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)
        self.assertEqual(data["kind"], "BROADCAST")
        self.assertEqual(data["title"], "Staff meeting")
        self.assertEqual(data["reply_mode"], "PRIVATE_REPLIES")
        self.assertEqual(data["recipient_count"], 2)
        self.assertEqual({r["name"] for r in data["recipients"]}, {"Funke Adeyemi", "Ibrahim Musa"})
        self.assertEqual(data["role"], "SENDER")
        self.assertNotIn("participants", data)
        self.assertEqual(audit_events("MESSAGE_BROADCAST_SENT").count(), 1)

    def test_only_staff_can_be_recipients(self):
        for user in (self.mother.user, self.tunde.user):
            response = self.send(self.lekki_head, recipients=[self.funke.pk, user.pk])
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Conversation.objects.exists())

    def test_the_senders_own_id_is_dropped_and_an_empty_list_is_refused(self):
        ok = self.send(self.lekki_head, recipients=[self.lekki_head.pk, self.funke.pk])
        self.assertEqual(self.results(ok)["recipient_count"], 1)
        self.assertEqual(self.send(self.lekki_head, recipients=[]).status_code, 400)
        self.assertEqual(self.send(self.lekki_head, recipients=[self.lekki_head.pk]).status_code, 400)

    def test_out_of_reach_recipients_are_refused_like_ones_that_do_not_exist(self):
        out = self.send(self.lekki_head, recipients=[self.tola.pk])
        gone = self.send(self.lekki_head, recipients=[MISSING])
        self.assertEqual(out.status_code, 400)
        self.assertEqual(out.data, gone.data)

    def test_a_thousand_recipients_at_most(self):
        response = self.send(self.admin, recipients=list(range(1, 1002)))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "MESSAGE_RECIPIENTS_INVALID")

    def test_a_branch_bound_sender_cannot_broadcast_school_wide(self):
        response = self.send(self.lekki_head, recipients=[self.funke.pk], branch=None)
        self.assertEqual(response.status_code, 403)

    def test_the_reply_setting_defaults_to_one_way(self):
        response = self.post(self.lekki_head, "messages-broadcast-create", {
            "title": "Notice", "body": "Hello", "branch": self.lekki.pk,
            "recipients": [self.funke.pk],
        })
        self.assertEqual(self.results(response)["reply_mode"], "ONE_WAY")

    def test_a_title_and_a_message_are_required(self):
        self.assertEqual(self.send(self.lekki_head, recipients=[self.funke.pk], title=" ").status_code, 400)
        self.assertEqual(self.send(self.lekki_head, recipients=[self.funke.pk], body=" ").status_code, 400)

    def test_the_sending_key_is_required(self):
        self.assertEqual(self.send(self.funke, recipients=[self.ibrahim.pk]).status_code, 403)

    def test_a_retry_with_the_same_token_sends_once(self):
        token = str(uuid.uuid4())
        first = self.send(self.lekki_head, recipients=[self.funke.pk], client_token=token)
        again = self.send(self.lekki_head, recipients=[self.funke.pk], client_token=token)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(Conversation.objects.filter(kind="BROADCAST").count(), 1)


class RecipientViewTests(MessagesFixture):
    """What a recipient and the sender each read of a broadcast."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim, cls.ngozi])

    def test_a_recipient_sees_how_many_it_went_to_and_not_who(self):
        data = self.results(self.get(self.funke, "messages-conversation", pk=self.notice.pk))
        self.assertEqual(data["recipient_count"], 3)
        self.assertNotIn("recipients", data)
        self.assertEqual(data["role"], "RECIPIENT")
        self.assertEqual(data["display_name"], "Staff meeting")
        self.assertIsNone(data["counterpart"])

    def test_the_sender_sees_the_list(self):
        data = self.results(self.get(self.lekki_head, "messages-conversation", pk=self.notice.pk))
        self.assertEqual(len(data["recipients"]), 3)

    def test_a_row_in_the_inbox_does_not_leak_the_count_as_a_member_count(self):
        row = self.results(self.get(self.funke, "messages-conversations", {"kind": "broadcast"}))[0]
        self.assertIsNone(row["member_count"])

    def test_recipients_cannot_post_into_the_broadcast(self):
        response = self.post(self.funke, "messages-messages", {"body": "Me too"}, pk=self.notice.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CONVERSATION_READ_ONLY")
        row = self.results(self.get(self.funke, "messages-conversations"))[0]
        self.assertFalse(row["can_send"])
        self.assertTrue(row["read_only"])

    def test_the_sender_can_follow_up_in_their_own_broadcast(self):
        response = self.post(self.lekki_head, "messages-messages", {"body": "Room changed"}, pk=self.notice.pk)
        self.assertEqual(response.status_code, 201)

    def test_a_staff_member_who_was_not_addressed_cannot_open_it(self):
        for user in (self.tola, self.reader, self.admin):
            self.assertEqual(self.get(user, "messages-conversation", pk=self.notice.pk).status_code, 404)

    def test_everyone_addressed_reads_the_message(self):
        page = self.results(self.get(self.ngozi, "messages-messages", pk=self.notice.pk))
        self.assertEqual([m["body"] for m in page["results"]], ["Friday at 2pm"])


class OneWayTests(MessagesFixture):
    """A one-way broadcast takes no replies."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim], reply_mode="ONE_WAY")

    def test_a_reply_to_a_one_way_broadcast_is_refused(self):
        response = self.post(self.funke, "messages-broadcast-reply", {"body": "Ok"}, pk=self.notice.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "BROADCAST_ONE_WAY")
        self.assertFalse(Conversation.objects.filter(kind="BROADCAST_REPLY").exists())

    def test_it_says_so_on_the_row(self):
        row = self.results(self.get(self.funke, "messages-conversations"))[0]
        self.assertEqual(row["reply_mode"], "ONE_WAY")
        self.assertEqual(row["read_only_reason"], "This broadcast does not take replies.")


class PrivateReplyTests(MessagesFixture):
    """Replies stay between the recipient and the sender."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.notice = cls.broadcast(cls.lekki_head, [cls.funke, cls.ibrahim, cls.ngozi])

    def reply(self, user, text="Noted", pk=None):
        return self.post(user, "messages-broadcast-reply", {"body": text}, pk=pk or self.notice.pk)

    def test_a_reply_opens_a_private_conversation_with_the_sender(self):
        response = self.reply(self.funke, "I will attend")
        self.assertEqual(response.status_code, 201, response.data)
        thread = self.results(response)["conversation"]
        self.assertEqual(thread["kind"], "BROADCAST_REPLY")
        self.assertEqual(thread["counterpart"]["name"], "School Admin")
        self.assertEqual(self.results(response)["message"]["body"], "I will attend")
        reply = Conversation.objects.get(kind="BROADCAST_REPLY")
        self.assertEqual(reply.pair_key, f"reply:{self.notice.pk}:{self.funke.pk}")
        self.assertEqual(reply.broadcast_id, self.notice.pk)
        self.assertEqual(
            set(Participant.objects.filter(conversation=reply).values_list("user_id", flat=True)),
            {self.lekki_head.pk, self.funke.pk},
        )

    def test_only_the_sender_and_that_recipient_see_the_reply(self):
        reply_id = self.results(self.reply(self.funke))["conversation"]["id"]
        for name in ("messages-conversation", "messages-messages"):
            self.assertEqual(self.get(self.funke, name, pk=reply_id).status_code, 200)
            self.assertEqual(self.get(self.lekki_head, name, pk=reply_id).status_code, 200)
            for other in (self.ibrahim, self.ngozi, self.admin, self.tola):
                self.assertEqual(self.get(other, name, pk=reply_id).status_code, 404, other.email)
        # Not in anyone else's inbox, search or unread count.
        for other in (self.ibrahim, self.ngozi):
            ids = {r["id"] for r in self.results(self.get(other, "messages-conversations"))}
            self.assertNotIn(reply_id, ids)
            self.assertEqual(
                self.results(self.get(other, "messages-conversations", {"search": "Noted"})), [],
            )
            self.assertEqual(self.results(self.get(other, "messages-unread-count"))["total"], 1)

    def test_the_sender_has_the_reply_unread_in_the_broadcast_tab(self):
        self.reply(self.funke)
        counts = self.results(self.get(self.lekki_head, "messages-unread-count"))
        self.assertEqual(counts["broadcast"], 1)
        rows = self.results(self.get(self.lekki_head, "messages-conversations", {"kind": "broadcast"}))
        self.assertEqual({r["kind"] for r in rows}, {"BROADCAST", "BROADCAST_REPLY"})

    def test_a_second_reply_from_the_same_recipient_uses_the_same_thread(self):
        first = self.results(self.reply(self.funke, "One"))["conversation"]["id"]
        second = self.reply(self.funke, "Two")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(self.results(second)["conversation"]["id"], first)
        self.assertEqual(Conversation.objects.filter(kind="BROADCAST_REPLY").count(), 1)
        other = self.results(self.reply(self.ibrahim, "Mine"))["conversation"]["id"]
        self.assertNotEqual(other, first)

    def test_the_sender_answers_in_the_thread(self):
        thread = self.results(self.reply(self.funke))["conversation"]["id"]
        sent = self.post(self.lekki_head, "messages-messages", {"body": "Thanks"}, pk=thread)
        self.assertEqual(sent.status_code, 201)
        page = self.results(self.get(self.funke, "messages-messages", pk=thread))
        self.assertEqual([m["body"] for m in page["results"]], ["Thanks", "Noted"])

    def test_the_sender_cannot_reply_to_their_own_broadcast(self):
        response = self.reply(self.lekki_head)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CONVERSATION_READ_ONLY")

    def test_a_reply_is_only_for_a_broadcast(self):
        dm = self.direct(self.funke, self.ibrahim)
        self.assertEqual(self.reply(self.funke, pk=dm.pk).status_code, 404)

    def test_a_reply_needs_a_message(self):
        self.assertEqual(self.reply(self.funke, "  ").status_code, 400)
        self.assertFalse(Conversation.objects.filter(kind="BROADCAST_REPLY").exists())

    def test_a_retried_reply_stores_one_message(self):
        token = str(uuid.uuid4())
        body = {"body": "Noted", "client_token": token}
        first = self.post(self.funke, "messages-broadcast-reply", body, pk=self.notice.pk)
        again = self.post(self.funke, "messages-broadcast-reply", body, pk=self.notice.pk)
        self.assertEqual((first.status_code, again.status_code), (201, 200))

    def test_an_inspector_of_the_broadcast_never_reaches_the_replies(self):
        reply_id = self.results(self.reply(self.funke, "private words"))["conversation"]["id"]
        detail = self.get(self.admin, "messages-oversight-conversation", pk=self.notice.pk)
        self.assertEqual(detail.status_code, 200)
        page = self.results(self.get(self.admin, "messages-oversight-messages", pk=self.notice.pk))
        self.assertEqual([m["body"] for m in page["results"]], ["Friday at 2pm"])
        missing = self.get(self.admin, "messages-oversight-conversation", pk=MISSING)
        for name in ("messages-oversight-conversation", "messages-oversight-messages"):
            hidden = self.get(self.admin, name, pk=reply_id)
            self.assertEqual(hidden.status_code, 404)
            self.assertEqual(hidden.data, missing.data)
        listed = self.results(self.get(self.admin, "messages-oversight-broadcasts"))
        self.assertEqual([r["id"] for r in listed], [self.notice.pk])

    def test_the_replying_recipient_is_alerted_to_the_senders_answer_only(self):
        reply = Conversation.objects.get(pk=self.results(self.reply(self.funke))["conversation"]["id"])
        message = self.say(reply, self.lekki_head, "Thanks")
        self.assertEqual({p.user_id for p in visibility.alert_recipients(message)}, {self.funke.pk})
