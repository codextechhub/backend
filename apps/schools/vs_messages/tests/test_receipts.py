"""Read receipts: what the sender of a message is told about who has read it.

At Brightfield, Mrs. Funke Adeyemi writes to Mr. Musa, to Tunde, to Tunde's
mother and to her Maths team. She sees one tick (``sent``) until the people she
wrote to have read her message and two (``read``) once they all have; in a group
the receipt also says how many have, for "Read by 1 of 2". A parent only
watching Tunde's conversation and a leader inspecting a group never turn a tick,
and are never shown one. At Sunrise, a school with one branch, the same rules
hold on their own setting.
"""
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_messages.models import Message, Participant
from schools.vs_messages.serializers import MessageSerializer
from schools.vs_messages.services import (
    broadcasts,
    config,
    conversations,
    groups,
    messages,
    receipts,
    visibility,
)
from schools.vs_messages.throttles import MessageSendThrottle
from schools.vs_staff.models import StaffProfile
from schools.vs_students.models import StudentGuardian
from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_user.models import User

from .base import MessagesFixture
from .test_family import switch

KEY = "school.messages."
MISSING = 999999


def visibility_access(tenant, user, conversation, surface):
    """The caller's standing in *conversation* on *surface*, at any school."""
    return visibility.resolve(tenant, user, conversation.pk, surface=surface)


def receipt(state, read, total):
    return {"state": state, "read_count": read, "recipient_count": total}


class ReceiptFixture(MessagesFixture):
    """One conversation of every kind, each opened by Mrs. Adeyemi or her colleagues."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.messages")
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "Please send the scheme of work")
        cls.family = cls.family_chat(cls.funke, cls.tunde, cls.mother, "Tunde did well today")
        cls.pupil = cls.pupil_chat(cls.funke, cls.tunde, "Please see me about the quiz")
        cls.team = cls.group(cls.funke, [cls.ibrahim, cls.ngozi], name="Maths team", body="Welcome")
        cls.notice = cls.broadcast(
            cls.lekki_head, [cls.funke, cls.ibrahim, cls.ngozi], title="Sports day", body="Friday at 9",
        )
        cls.eze_chat = cls.family_chat(cls.ngozi, cls.chiamaka, cls.eze, "Welcome to the school")

    # ── helpers ──

    @staticmethod
    def newest(conversation, sender=None):
        rows = Message.objects.filter(conversation=conversation).order_by("-id")
        return (rows.filter(sender=sender) if sender else rows).first()

    def read(self, user, conversation, surface="staff"):
        messages.mark_read(self.access(user, conversation, surface), user)

    def page(self, user, conversation, route="messages-messages", **params):
        response = self.get(user, route, params, pk=conversation.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return self.results(response)

    def receipt_of(self, user, conversation, message, route="messages-messages"):
        """The ``receipt`` the sender is served for *message*, or None when the key is absent."""
        rows = {m["id"]: m for m in self.page(user, conversation, route)["results"]}
        return rows[message.pk].get("receipt")

    def hide(self, field="read_receipts", *, role=None, read=False, write=False):
        set_field_access(role or self.teacher_role, KEY + field, read=read, write=write)


class DirectTests(ReceiptFixture):
    """A staff conversation has one reader, so the tick turns when the other person reads."""
    def test_sent_until_the_other_person_reads(self):
        sent = self.newest(self.dm, self.funke)
        self.assertEqual(self.receipt_of(self.funke, self.dm, sent), receipt("sent", 0, 1))
        self.read(self.ibrahim, self.dm)
        self.assertEqual(self.receipt_of(self.funke, self.dm, sent), receipt("read", 1, 1))

    def test_a_reply_has_its_own_receipt_and_only_the_sender_gets_one(self):
        self.read(self.ibrahim, self.dm)
        reply = self.say(self.dm, self.ibrahim, "Will do tonight")
        rows = {m["id"]: m for m in self.page(self.funke, self.dm)["results"]}
        self.assertNotIn("receipt", rows[reply.pk])
        self.assertEqual(rows[self.newest(self.dm, self.funke).pk]["receipt"]["state"], "read")
        mine = {m["id"]: m for m in self.page(self.ibrahim, self.dm)["results"]}
        self.assertEqual(mine[reply.pk]["receipt"], receipt("sent", 0, 1))
        self.assertNotIn("receipt", mine[self.newest(self.dm, self.funke).pk])

    def test_a_watermark_below_the_message_is_not_read(self):
        first = self.newest(self.dm, self.funke)
        self.read(self.ibrahim, self.dm)
        later = self.say(self.dm, self.funke, "And the exam paper too")
        self.assertEqual(self.receipt_of(self.funke, self.dm, first), receipt("read", 1, 1))
        self.assertEqual(self.receipt_of(self.funke, self.dm, later), receipt("sent", 0, 1))

    def test_the_send_response_carries_the_receipt(self):
        response = self.post(self.funke, "messages-messages", {"body": "Thanks"}, pk=self.dm.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.results(response)["receipt"], receipt("sent", 0, 1))

    def test_a_retried_send_carries_the_receipt_of_the_stored_message(self):
        token = "5d1f7a4e-1b0c-4c63-9a53-0a1f2b3c4d5e"
        self.post(self.funke, "messages-messages", {"body": "Once", "client_token": token}, pk=self.dm.pk)
        self.read(self.ibrahim, self.dm)
        again = self.post(self.funke, "messages-messages", {"body": "Once", "client_token": token}, pk=self.dm.pk)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.results(again)["receipt"], receipt("read", 1, 1))

    def test_the_start_response_carries_the_receipt_and_the_flag(self):
        response = self.post(self.funke, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": self.ngozi.pk, "body": "Hello Ngozi",
        })
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)
        self.assertEqual(data["message"]["receipt"], receipt("sent", 0, 1))
        self.assertIs(data["conversation"]["receipts"], True)

    def test_a_start_without_a_message_has_no_message_to_carry_one(self):
        response = self.post(self.funke, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": self.ngozi.pk,
        })
        self.assertIsNone(self.results(response)["message"])

    def test_the_conversation_says_whether_ticks_are_drawn(self):
        detail = self.results(self.get(self.funke, "messages-conversation", pk=self.dm.pk))
        self.assertIs(detail["receipts"], True)

    def test_an_unreachable_reader_leaves_the_message_sent(self):
        User.objects.filter(pk=self.ibrahim.pk).update(is_active=False)
        sent = self.newest(self.dm, self.funke)
        self.read(self.funke, self.dm)
        self.assertEqual(self.receipt_of(self.funke, self.dm, sent), receipt("sent", 0, 0))


class FamilyTests(ReceiptFixture):
    """A parent conversation: Mrs. Adeyemi and Tunde's mother."""
    def test_staff_sees_read_after_the_guardian_reads(self):
        sent = self.newest(self.family, self.funke)
        self.assertEqual(self.receipt_of(self.funke, self.family, sent), receipt("sent", 0, 1))
        self.read(self.mother.user, self.family, "parent")
        self.assertEqual(self.receipt_of(self.funke, self.family, sent), receipt("read", 1, 1))

    def test_the_guardian_sees_read_after_staff_reads(self):
        said = self.say(self.family, self.mother.user, "Thank you, madam")
        self.assertEqual(
            self.receipt_of(self.mother.user, self.family, said, "parent-messages-messages"),
            receipt("sent", 0, 1),
        )
        self.read(self.funke, self.family)
        self.assertEqual(
            self.receipt_of(self.mother.user, self.family, said, "parent-messages-messages"),
            receipt("read", 1, 1),
        )

    def test_the_parent_send_response_carries_the_receipt(self):
        response = self.post(
            self.mother.user, "parent-messages-messages", {"body": "See you Friday"}, pk=self.family.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.results(response)["receipt"], receipt("sent", 0, 1))

    def test_the_parent_start_response_carries_the_receipt_and_the_detail_the_flag(self):
        response = self.post(self.mother.user, "parent-messages-conversations", {
            "child": self.tunde.pk, "staff": self.ibrahim.pk, "body": "Can he catch up?",
        })
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)
        self.assertEqual(data["message"]["receipt"], receipt("sent", 0, 1))
        self.assertIs(data["conversation"]["receipts"], True)
        detail = self.results(self.get(
            self.mother.user, "parent-messages-conversation", pk=self.family.pk,
        ))
        self.assertIs(detail["receipts"], True)

    def test_a_list_row_does_not_carry_the_flag(self):
        rows = self.results(self.get(self.mother.user, "parent-messages-conversations"))
        self.assertNotIn("receipts", rows[0])

    def test_an_unlinked_guardian_is_not_a_reader(self):
        sent = self.newest(self.family, self.funke)
        StudentGuardian.objects.filter(student=self.tunde, guardian=self.mother).delete()
        self.assertEqual(self.receipt_of(self.funke, self.family, sent), receipt("sent", 0, 0))

    def test_a_reader_who_left_staff_does_not_hold_the_guardian_back(self):
        said = self.say(self.family, self.mother.user, "Thank you")
        StaffProfile.objects.filter(user=self.funke).update(employment_status="RESIGNED")
        self.assertEqual(
            self.receipt_of(self.mother.user, self.family, said, "parent-messages-messages"),
            receipt("sent", 0, 0),
        )


class PupilTests(ReceiptFixture):
    """A pupil conversation, from the staff end and from the student app."""
    def test_staff_sees_read_after_the_pupil_reads(self):
        sent = self.newest(self.pupil, self.funke)
        self.assertEqual(self.receipt_of(self.funke, self.pupil, sent), receipt("sent", 0, 1))
        self.read(self.tunde.user, self.pupil, "student")
        self.assertEqual(self.receipt_of(self.funke, self.pupil, sent), receipt("read", 1, 1))

    def test_the_pupil_sees_read_after_staff_reads(self):
        said = self.say(self.pupil, self.tunde.user, "Yes madam")
        self.assertEqual(
            self.receipt_of(self.tunde.user, self.pupil, said, "student-messages-messages"),
            receipt("sent", 0, 1),
        )
        self.read(self.funke, self.pupil)
        self.assertEqual(
            self.receipt_of(self.tunde.user, self.pupil, said, "student-messages-messages"),
            receipt("read", 1, 1),
        )

    def test_the_student_routes_carry_receipts_and_the_flag(self):
        sent = self.post(
            self.tunde.user, "student-messages-messages", {"body": "On my way"}, pk=self.pupil.pk,
        )
        self.assertEqual(self.results(sent)["receipt"], receipt("sent", 0, 1))
        started = self.post(self.tunde.user, "student-messages-conversations", {
            "staff": self.ibrahim.pk, "body": "Sir, a question",
        })
        self.assertEqual(started.status_code, 201, started.data)
        self.assertEqual(self.results(started)["message"]["receipt"], receipt("sent", 0, 1))
        detail = self.results(self.get(self.tunde.user, "student-messages-conversation", pk=self.pupil.pk))
        self.assertIs(detail["receipts"], True)

    def test_a_pupil_off_the_roll_is_not_a_reader(self):
        sent = self.newest(self.pupil, self.funke)
        from schools.vs_students.models import Student

        Student.objects.filter(pk=self.tunde.pk).update(status="WITHDRAWN")
        self.assertEqual(self.receipt_of(self.funke, self.pupil, sent), receipt("sent", 0, 0))


class WatchingAndInspectionTests(ReceiptFixture):
    """Reading a conversation one is not part of never shows, or moves, a tick."""
    def watermarks(self):
        return list(Participant.objects.order_by("pk").values_list("pk", "last_read_message_id"))

    def test_a_watching_parent_sees_no_receipt_and_the_flag_is_false(self):
        self.say(self.pupil, self.tunde.user, "Yes madam")
        data = self.page(self.father.user, self.pupil, "parent-messages-messages")
        self.assertGreaterEqual(len(data["results"]), 2)
        for row in data["results"]:
            self.assertNotIn("receipt", row)
        detail = self.results(self.get(self.father.user, "parent-messages-conversation", pk=self.pupil.pk))
        self.assertIs(detail["receipts"], False)
        polled = self.page(self.father.user, self.pupil, "parent-messages-messages", after=0)
        self.assertNotIn("receipts", polled)
        for row in polled["results"]:
            self.assertNotIn("receipt", row)

    def test_a_watching_parent_opening_it_never_turns_a_pupils_message_read(self):
        sent = self.newest(self.pupil, self.funke)
        before = self.watermarks()
        self.page(self.father.user, self.pupil, "parent-messages-messages")
        response = self.post(self.father.user, "parent-messages-read", {}, pk=self.pupil.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.watermarks(), before)
        self.assertEqual(self.receipt_of(self.funke, self.pupil, sent), receipt("sent", 0, 1))

    def test_an_inspector_sees_no_receipt_and_inspecting_turns_nothing_read(self):
        sent = self.newest(self.team, self.funke)
        before = self.watermarks()
        data = self.page(self.admin, self.team, "messages-oversight-messages")
        self.assertTrue(data["results"])
        for row in data["results"]:
            self.assertNotIn("receipt", row)
        polled = self.page(self.admin, self.team, "messages-oversight-messages", after=0)
        self.assertNotIn("receipts", polled)
        self.assertEqual(self.watermarks(), before)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("sent", 0, 2))

    def test_the_service_gives_an_inspector_nothing(self):
        from schools.vs_messages.services import oversight

        access = oversight.resolve_any(self.tenant, self.admin, self.team.pk)
        rows = list(Message.objects.filter(conversation=self.team))
        self.assertEqual(receipts.for_messages(access, self.admin, rows, settings=config.MessagingSettings()), {})
        watching = self.access(self.father.user, self.pupil, "parent")
        self.assertEqual(
            receipts.for_messages(watching, self.father.user, rows, settings=config.MessagingSettings()), {},
        )


class GroupTests(ReceiptFixture):
    """A group counts everyone who could see the message, and turns read only when all have."""
    def test_read_only_when_every_member_has_read(self):
        sent = self.newest(self.team, self.funke)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("sent", 0, 2))
        self.read(self.ibrahim, self.team)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("sent", 1, 2))
        self.read(self.ngozi, self.team)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("read", 2, 2))

    def test_a_member_who_joined_after_the_message_is_not_counted(self):
        sent = self.newest(self.team, self.funke)
        groups.add_members(self.access(self.funke, self.team), self.funke, [self.head_staff.user_id])
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("sent", 0, 2))
        later = self.say(self.team, self.funke, "Welcome Mr. head")
        self.assertEqual(self.receipt_of(self.funke, self.team, later), receipt("sent", 0, 3))
        self.read(self.ibrahim, self.team)
        self.read(self.ngozi, self.team)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("read", 2, 2))
        self.assertEqual(self.receipt_of(self.funke, self.team, later), receipt("sent", 2, 3))

    def test_a_removed_member_is_not_counted(self):
        sent = self.newest(self.team, self.funke)
        groups.remove_member(self.access(self.funke, self.team), self.funke, self.ngozi.pk)
        self.read(self.ibrahim, self.team)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("read", 1, 1))

    def test_a_deactivated_member_is_not_counted(self):
        sent = self.newest(self.team, self.funke)
        User.objects.filter(pk=self.ngozi.pk).update(is_active=False)
        self.read(self.ibrahim, self.team)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("read", 1, 1))

    def test_an_off_roll_member_is_not_counted(self):
        sent = self.newest(self.team, self.funke)
        StaffProfile.objects.filter(user=self.ngozi).update(employment_status="TERMINATED")
        self.read(self.ibrahim, self.team)
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("read", 1, 1))

    def test_a_member_reads_their_own_messages_receipts_not_the_owners(self):
        said = self.say(self.team, self.ibrahim, "Ready")
        rows = {m["id"]: m for m in self.page(self.ibrahim, self.team)["results"]}
        self.assertEqual(rows[said.pk]["receipt"], receipt("sent", 0, 2))
        welcome = self.newest(self.team, self.funke)
        self.assertNotIn("receipt", rows[welcome.pk])

    def test_a_member_who_rejoined_counts_from_the_new_join_point(self):
        sent = self.newest(self.team, self.funke)
        groups.remove_member(self.access(self.funke, self.team), self.funke, self.ngozi.pk)
        groups.add_members(self.access(self.funke, self.team), self.funke, [self.ngozi.pk])
        self.assertEqual(self.receipt_of(self.funke, self.team, sent), receipt("sent", 0, 1))


class BroadcastTests(ReceiptFixture):
    """A broadcast counts its recipients; a private reply is a separate direct conversation."""
    def test_the_sender_sees_how_many_recipients_have_read(self):
        sent = self.newest(self.notice, self.lekki_head)
        self.assertEqual(self.receipt_of(self.lekki_head, self.notice, sent), receipt("sent", 0, 3))
        self.read(self.funke, self.notice)
        self.read(self.ibrahim, self.notice)
        self.assertEqual(self.receipt_of(self.lekki_head, self.notice, sent), receipt("sent", 2, 3))
        self.read(self.ngozi, self.notice)
        self.assertEqual(self.receipt_of(self.lekki_head, self.notice, sent), receipt("read", 3, 3))

    def test_a_recipient_has_no_receipt_on_the_broadcast(self):
        rows = self.page(self.funke, self.notice)["results"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("receipt", row)

    def test_a_private_reply_is_its_own_direct_receipt(self):
        thread, reply, _ = broadcasts.reply_privately(self.tenant, self.funke, self.notice.pk, body="Noted")
        self.assertEqual(
            self.receipt_of(self.funke, thread, reply), receipt("sent", 0, 1),
        )
        self.read(self.lekki_head, thread)
        self.assertEqual(self.receipt_of(self.funke, thread, reply), receipt("read", 1, 1))
        original = self.newest(self.notice, self.lekki_head)
        self.assertEqual(self.receipt_of(self.lekki_head, self.notice, original), receipt("sent", 0, 3))

    def test_the_reply_response_carries_the_receipt(self):
        response = self.post(self.funke, "messages-broadcast-reply", {"body": "Will attend"}, pk=self.notice.pk)
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)
        self.assertEqual(data["message"]["receipt"], receipt("sent", 0, 1))
        self.assertIs(data["conversation"]["receipts"], True)


class SettingTests(ReceiptFixture):
    """The school setting is all or nothing, and belongs to one school."""
    def test_off_removes_every_receipt_everywhere(self):
        switch(self, "messages.read_receipts", False)
        sent = self.newest(self.dm, self.funke)
        self.assertIsNone(self.receipt_of(self.funke, self.dm, sent))
        self.assertNotIn("receipt", self.results(
            self.post(self.funke, "messages-messages", {"body": "Hello"}, pk=self.dm.pk),
        ))
        self.assertIs(self.results(self.get(self.funke, "messages-conversation", pk=self.dm.pk))["receipts"], False)
        polled = self.page(self.funke, self.dm, after=0)
        self.assertNotIn("receipts", polled)
        for row in polled["results"]:
            self.assertNotIn("receipt", row)

    def test_off_removes_them_from_both_portals(self):
        switch(self, "messages.read_receipts", False)
        said = self.say(self.family, self.mother.user, "Thank you")
        self.read(self.funke, self.family)
        self.assertIsNone(self.receipt_of(self.mother.user, self.family, said, "parent-messages-messages"))
        parent = self.results(self.get(self.mother.user, "parent-messages-conversation", pk=self.family.pk))
        self.assertIs(parent["receipts"], False)
        pupil_said = self.say(self.pupil, self.tunde.user, "Yes madam")
        self.assertIsNone(self.receipt_of(self.tunde.user, self.pupil, pupil_said, "student-messages-messages"))
        student = self.results(self.get(self.tunde.user, "student-messages-conversation", pk=self.pupil.pk))
        self.assertIs(student["receipts"], False)

    def test_off_at_one_school_leaves_another_school_alone(self):
        switch(self, "messages.read_receipts", False)
        convo, message, _ = conversations.start_family_from_staff(
            self.solo.tenant, self.ada, kind="FAMILY", student_id=self.bola.pk,
            guardian_id=self.bayo.pk, body="Welcome to Sunrise",
        )
        rows = {m["id"]: m for m in self.page(self.ada, convo)["results"]}
        self.assertEqual(rows[message.pk]["receipt"], receipt("sent", 0, 1))

    def test_a_school_with_one_branch_turns_ticks_like_any_other(self):
        convo, message, _ = conversations.start_family_from_staff(
            self.solo.tenant, self.ada, kind="FAMILY", student_id=self.bola.pk,
            guardian_id=self.bayo.pk, body="Welcome to Sunrise",
        )
        messages.mark_read(
            visibility_access(self.solo.tenant, self.bayo.user, convo, "parent"), self.bayo.user,
        )
        rows = {m["id"]: m for m in self.page(self.ada, convo)["results"]}
        self.assertEqual(rows[message.pk]["receipt"], receipt("read", 1, 1))

    def test_the_default_is_on(self):
        self.assertTrue(config.MessagingSettings().read_receipts)
        self.assertTrue(config.load_settings(self.tenant).read_receipts)


class FieldAccessTests(ReceiptFixture):
    """The Read receipts switch: open by default, read-only by nature, hidden on every route."""
    def test_open_by_default(self):
        sent = self.newest(self.dm, self.funke)
        self.assertEqual(self.receipt_of(self.funke, self.dm, sent), receipt("sent", 0, 1))
        self.assertIn("receipts", self.results(self.get(self.funke, "messages-conversation", pk=self.dm.pk)))

    def test_a_read_only_switch_still_shows_receipts_and_names_it_read_only(self):
        self.hide(read=True, write=False)
        sent = self.newest(self.dm, self.funke)
        self.assertEqual(self.receipt_of(self.funke, self.dm, sent), receipt("sent", 0, 1))
        detail = self.results(self.get(self.funke, "messages-conversation", pk=self.dm.pk))
        self.assertIs(detail["receipts"], True)
        self.assertIn("receipts", detail["_read_only_fields"])

    def test_hidden_removes_the_receipt_from_the_list(self):
        self.hide()
        sent = self.newest(self.dm, self.funke)
        self.assertIsNone(self.receipt_of(self.funke, self.dm, sent))
        self.assertNotIn("receipt", str(self.page(self.funke, self.dm)))

    def test_hidden_removes_it_from_the_send_and_start_responses(self):
        self.hide()
        sent = self.post(self.funke, "messages-messages", {"body": "Thanks"}, pk=self.dm.pk)
        self.assertNotIn("receipt", self.results(sent))
        started = self.post(self.funke, "messages-conversations", {
            "kind": "STAFF_DIRECT", "recipient": self.ngozi.pk, "body": "Hi",
        })
        data = self.results(started)
        self.assertNotIn("receipt", data["message"])
        self.assertNotIn("receipts", data["conversation"])
        reply = self.post(self.funke, "messages-broadcast-reply", {"body": "Noted"}, pk=self.notice.pk)
        self.assertNotIn("receipt", self.results(reply)["message"])

    def test_hidden_removes_it_from_the_conversation_and_the_polling_route(self):
        self.hide()
        detail = self.results(self.get(self.funke, "messages-conversation", pk=self.dm.pk))
        self.assertNotIn("receipts", detail)
        self.assertIn("unread_count", detail)
        polled = self.page(self.funke, self.dm, after=0)
        self.assertNotIn("receipts", polled)
        self.assertNotIn("receipt", str(polled))
        self.assertTrue(polled["results"])

    def test_hidden_for_one_role_leaves_another_role_its_receipts(self):
        self.hide()
        sent = self.newest(self.team, self.funke)
        groups.add_members(self.access(self.funke, self.team), self.funke, [self.lekki_head.pk])
        later = self.say(self.team, self.lekki_head, "Glad to be here")
        self.assertEqual(self.receipt_of(self.lekki_head, self.team, later), receipt("sent", 0, 3))
        self.assertIsNone(self.receipt_of(self.funke, self.team, sent))

    def test_the_portals_are_not_reached_by_the_switch(self):
        self.hide(role=self.teacher_role)
        said = self.say(self.family, self.mother.user, "Thank you")
        self.assertEqual(
            self.receipt_of(self.mother.user, self.family, said, "parent-messages-messages"),
            receipt("sent", 0, 1),
        )


class PollingTests(ReceiptFixture):
    """``?after=<id>``: what is new, and the receipts of the caller's own recent messages."""
    def test_it_returns_the_newer_messages_oldest_first_and_the_receipts(self):
        welcome = self.newest(self.team, self.funke)
        first = self.say(self.team, self.ibrahim, "Ready when you are")
        second = self.say(self.team, self.funke, "Good, start Monday")
        self.read(self.ibrahim, self.team)
        data = self.page(self.funke, self.team, after=welcome.pk)
        self.assertEqual([m["id"] for m in data["results"]], [first.pk, second.pk])
        self.assertFalse(data["has_more"])
        by_id = {r["id"]: r for r in data["receipts"]}
        self.assertEqual(set(by_id), {welcome.pk, second.pk})
        self.assertEqual(set(by_id[welcome.pk]), {"id", "state", "read_count", "recipient_count"})
        self.assertEqual(by_id[welcome.pk]["read_count"], 1)
        self.assertEqual(by_id[second.pk]["recipient_count"], 2)
        self.assertEqual([r["id"] for r in data["receipts"]], sorted(by_id))
        self.assertNotIn("receipt", data["results"][0])
        self.assertEqual(data["results"][1]["receipt"]["read_count"], 1)

    def test_nothing_new_still_refreshes_the_receipts(self):
        sent = self.newest(self.dm, self.funke)
        self.read(self.ibrahim, self.dm)
        data = self.page(self.funke, self.dm, after=sent.pk)
        self.assertEqual(data["results"], [])
        self.assertEqual(data["receipts"], [{"id": sent.pk, **receipt("read", 1, 1)}])

    def test_it_respects_the_join_watermark_of_a_late_member(self):
        before = self.newest(self.team, self.funke)
        groups.add_members(self.access(self.funke, self.team), self.funke, [self.head_staff.user_id])
        after = self.say(self.team, self.funke, "Welcome")
        data = self.page(self.lekki_head, self.team, after=0)
        self.assertEqual([m["id"] for m in data["results"]], [after.pk])
        self.assertNotIn(before.pk, [m["id"] for m in data["results"]])

    def test_a_page_holds_at_most_a_hundred_and_says_there_is_more(self):
        start = self.newest(self.team, self.funke)
        Message.objects.bulk_create([
            Message(tenant=self.tenant, conversation=self.team, sender=self.ibrahim, body=f"m{i}")
            for i in range(101)
        ])
        data = self.page(self.funke, self.team, after=start.pk)
        self.assertEqual(len(data["results"]), 100)
        self.assertTrue(data["has_more"])
        ids = [m["id"] for m in data["results"]]
        self.assertEqual(ids, sorted(ids))
        rest = self.page(self.funke, self.team, after=ids[-1])
        self.assertEqual(len(rest["results"]), 1)
        self.assertFalse(rest["has_more"])

    def test_without_after_the_cursor_page_is_unchanged(self):
        data = self.page(self.funke, self.team)
        self.assertEqual(set(data), {"results", "has_more", "next_before"})

    def test_the_portals_poll_too(self):
        said = self.say(self.family, self.mother.user, "Thank you")
        self.read(self.funke, self.family)
        parent = self.page(self.mother.user, self.family, "parent-messages-messages", after=0)
        self.assertEqual([r for r in parent["receipts"] if r["id"] == said.pk], [{"id": said.pk, **receipt("read", 1, 1)}])
        self.assertTrue(parent["results"])
        pupil_said = self.say(self.pupil, self.tunde.user, "Yes madam")
        student = self.page(self.tunde.user, self.pupil, "student-messages-messages", after=0)
        self.assertEqual([r["id"] for r in student["receipts"]], [pupil_said.pk])

    def test_an_unreachable_conversation_is_404_on_every_polling_route(self):
        other, _, _ = conversations.start_staff_direct(
            self.solo.tenant, self.ada, recipient_id=self.solo_admin.pk, body="Hello",
        )
        for user, route, pk in (
            (self.funke, "messages-messages", other.pk),
            (self.funke, "messages-messages", MISSING),
            (self.mother.user, "parent-messages-messages", other.pk),
            (self.mother.user, "parent-messages-messages", self.eze_chat.pk),
            (self.tunde.user, "student-messages-messages", other.pk),
            (self.tunde.user, "student-messages-messages", self.family.pk),
        ):
            with self.subTest(route=route, pk=pk):
                response = self.get(user, route, {"after": 0}, pk=pk)
                self.assertEqual(response.status_code, 404, response.data)

    def test_polling_does_not_count_against_the_send_limit(self):
        cache.clear()
        with mock.patch.object(MessageSendThrottle, "THROTTLE_RATES", {"message_send": "2/minute"}):
            for _ in range(6):
                self.assertEqual(
                    self.get(self.funke, "messages-messages", {"after": 0}, pk=self.dm.pk).status_code, 200,
                )
            first = self.post(self.funke, "messages-messages", {"body": "One"}, pk=self.dm.pk)
            second = self.post(self.funke, "messages-messages", {"body": "Two"}, pk=self.dm.pk)
            third = self.post(self.funke, "messages-messages", {"body": "Three"}, pk=self.dm.pk)
        cache.clear()
        self.assertEqual((first.status_code, second.status_code, third.status_code), (201, 201, 429))


class CostTests(ReceiptFixture):
    """A page costs the same for two members as for fourteen, and the service reads once."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.crowd = [
            cls.person(
                f"teacher{i}@brightfield.test", f"Teacher{i}", "Crowd", cls.teacher_role, cls.lekki, "Teacher",
            )
            for i in range(12)
        ]
        cls.big = cls.group(cls.funke, [cls.ibrahim, cls.ngozi, *cls.crowd], name="Whole staff", body="Welcome all")
        for index in range(5):
            cls.say(cls.big, cls.funke, f"Notice {index}")
            cls.say(cls.team, cls.funke, f"Notice {index}")

    def queries(self, conversation):
        self.get(self.funke, "messages-messages", pk=conversation.pk)  # warm the per-request caches
        with CaptureQueriesContext(connection) as small:
            self.get(self.funke, "messages-messages", pk=conversation.pk)
        return len(small)

    def test_a_page_costs_the_same_whatever_the_size_of_the_group(self):
        self.assertEqual(self.queries(self.team), self.queries(self.big))

    def test_receipts_add_a_single_query_for_any_number_of_messages(self):
        access = self.access(self.funke, self.big)
        rows = list(Message.objects.filter(conversation=self.big))
        self.assertGreater(len(rows), 5)
        with CaptureQueriesContext(connection) as captured:
            shown = receipts.for_messages(access, self.funke, rows, settings=config.MessagingSettings())
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(shown), len(rows))
        self.assertEqual({r["recipient_count"] for r in shown.values()}, {14})


class SerializerTests(ReceiptFixture):
    """The message serializer renders exactly what it is given."""
    def test_a_message_without_an_entry_has_no_receipt_key(self):
        message = self.newest(self.dm, self.funke)
        data = MessageSerializer(message, context={"user": self.funke}).data
        self.assertNotIn("receipt", data)
        shown = {message.pk: receipt("sent", 0, 1)}
        data = MessageSerializer(message, context={"user": self.funke, "receipts": shown}).data
        self.assertEqual(data["receipt"], receipt("sent", 0, 1))
