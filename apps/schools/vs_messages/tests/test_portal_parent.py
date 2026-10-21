"""The parent app's messages: what a parent reads, writes and starts.

At Brightfield, Mrs. Funke Bello is Tunde's mother and Mr. Kunle Bello his
primary guardian; Mrs. Ada Eze is Chiamaka's mother. Mrs. Bello is also Kemi's
aunt, so she has children at both branches. Tunde's class teacher is Mrs.
Adeyemi and Mr. Musa teaches him Mathematics; Mrs. Okoro is the Lekki office.
"""
from __future__ import annotations

import uuid

from schools.vs_messages.models import Conversation, Message, Participant
from schools.vs_messages.services import conversations
from schools.vs_students.constants import Relationship
from schools.vs_students.models import StudentGuardian

from .base import MessagesFixture
from .test_family import switch

MISSING = 999999
ROW_KEYS = {
    "id", "kind", "kind_label", "display_name", "counterpart", "child", "last_message",
    "last_activity_at", "unread_count", "read_only", "can_send", "read_only_reason",
}


class ParentMessagesFixture(MessagesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.link(cls, cls.kemi, cls.mother, primary=False, relationship=Relationship.OTHER)
        # Mrs. Bello and Mrs. Adeyemi talk about Tunde; Mrs. Bello has the last word unread.
        cls.family = cls.family_chat(cls.funke, cls.tunde, cls.mother, "Tunde did well in the test")
        cls.say(cls.family, cls.mother.user, "Thank you for telling me")
        cls.say(cls.family, cls.funke, "See you at the parents' meeting")
        # Tunde and Mrs. Adeyemi talk too; his father, the primary guardian, may read it.
        cls.pupil = cls.pupil_chat(cls.funke, cls.tunde, "Please see me about the quiz")
        cls.say(cls.pupil, cls.tunde.user, "Yes madam")
        # Mrs. Bello and the Ikeja office talk about Kemi.
        cls.kemi_chat = cls.family_chat(cls.segun, cls.kemi, cls.mother, "Kemi's fees are in")
        # Mrs. Eze and the Lekki office talk about Chiamaka.
        cls.eze_chat = cls.family_chat(cls.ngozi, cls.chiamaka, cls.eze, "Welcome to the school")

    def rows(self, user, **params):
        response = self.get(user, "parent-messages-conversations", params)
        self.assertEqual(response.status_code, 200, response.data)
        return {r["id"]: r for r in self.results(response)}, response

    def start(self, user, **body):
        return self.post(user, "parent-messages-conversations", body)

    def snapshot(self):
        return list(Participant.objects.order_by("pk").values_list(
            "pk", "user_id", "last_read_message_id", "left_at",
        ))


class ListTests(ParentMessagesFixture):
    """The parent's list: their own conversations and, read-only, the pupil ones they watch."""

    def test_a_parent_sees_their_own_conversations_in_a_narrow_shape(self):
        rows, response = self.rows(self.mother.user)
        self.assertEqual(set(rows), {self.family.pk, self.kemi_chat.pk})
        row = rows[self.family.pk]
        self.assertEqual(set(row), ROW_KEYS)
        self.assertEqual(row["kind"], "FAMILY")
        self.assertEqual(row["display_name"], "Funke Adeyemi")
        self.assertEqual(row["counterpart"]["label"], "Class teacher, JSS1 B")
        self.assertEqual(row["child"], {"id": self.tunde.pk, "name": "Tunde Bello", "class_name": "JSS1 B"})
        self.assertEqual(row["unread_count"], 1)
        self.assertTrue(row["can_send"])
        self.assertFalse(row["read_only"])
        self.assertEqual(row["last_message"]["preview"], "See you at the parents' meeting")
        self.assertFalse(row["last_message"]["mine"])
        self.assertEqual(response.data["unread_count"], 2)

    def test_the_label_follows_the_child_not_the_staff_member(self):
        rows, _ = self.rows(self.mother.user)
        self.assertEqual(rows[self.kemi_chat.pk]["counterpart"]["label"], "School office")

    def test_nothing_a_parent_does_not_need_is_in_the_row(self):
        row = self.rows(self.mother.user)[0][self.family.pk]
        for name in ("title", "role", "branch", "member_count", "reply_mode", "participants"):
            self.assertNotIn(name, row)
        self.assertEqual(set(row["counterpart"]), {"user_id", "name", "label"})

    def test_the_primary_guardian_watches_the_pupil_conversation_read_only(self):
        rows, response = self.rows(self.father.user)
        self.assertEqual(set(rows), {self.pupil.pk})
        row = rows[self.pupil.pk]
        self.assertEqual(row["kind"], "PUPIL")
        self.assertTrue(row["read_only"])
        self.assertFalse(row["can_send"])
        self.assertEqual(row["read_only_reason"], "Tunde's conversation. You can read it but not reply.")
        self.assertEqual(row["unread_count"], 0)
        self.assertEqual(response.data["unread_count"], 0)

    def test_a_guardian_who_is_not_primary_does_not_see_the_pupil_conversation(self):
        self.assertNotIn(self.pupil.pk, self.rows(self.mother.user)[0])

    def test_the_child_filter_narrows_the_list(self):
        rows, _ = self.rows(self.mother.user, child=self.tunde.pk)
        self.assertEqual(set(rows), {self.family.pk})
        rows, _ = self.rows(self.mother.user, child=self.kemi.pk)
        self.assertEqual(set(rows), {self.kemi_chat.pk})

    def test_a_child_that_is_not_theirs_is_the_portals_404(self):
        response = self.get(self.mother.user, "parent-messages-conversations", {"child": self.chiamaka.pk})
        self.assertEqual(response.status_code, 404)

    def test_unread_filter_and_search(self):
        rows, _ = self.rows(self.father.user, unread="true")
        self.assertEqual(rows, {})
        rows, _ = self.rows(self.mother.user, unread="true")
        self.assertEqual(set(rows), {self.family.pk, self.kemi_chat.pk})
        rows, _ = self.rows(self.mother.user, search="parents' meeting")
        self.assertEqual(set(rows), {self.family.pk})
        rows, _ = self.rows(self.mother.user, search="Ola")
        self.assertEqual(set(rows), {self.kemi_chat.pk})

    def test_a_conversation_started_but_not_yet_spoken_in_is_not_listed(self):
        empty, _, _ = conversations.start_family_from_staff(
            self.tenant, self.ibrahim, kind="FAMILY", student_id=self.tunde.pk,
            guardian_id=self.mother.pk,
        )
        self.assertNotIn(empty.pk, self.rows(self.mother.user)[0])

    def test_the_unread_count_route_excludes_what_is_only_watched(self):
        mother = self.results(self.get(self.mother.user, "parent-messages-unread-count"))
        father = self.results(self.get(self.father.user, "parent-messages-unread-count"))
        self.assertEqual(mother, {"total": 2})
        self.assertEqual(father, {"total": 0})


class AccessTests(ParentMessagesFixture):
    """Who gets in, and what ends access."""

    def test_routes_refuse_the_unlinked(self):
        for name, kwargs in (
            ("parent-messages-conversations", {}),
            ("parent-messages-conversation", {"pk": self.family.pk}),
            ("parent-messages-messages", {"pk": self.family.pk}),
            ("parent-messages-unread-count", {}),
            ("parent-messages-contacts", {}),
        ):
            with self.subTest(route=name):
                self.assertEqual(self.get(self.funke, name, **kwargs).status_code, 404)

    def test_another_familys_conversation_is_a_404(self):
        for name in ("parent-messages-conversation", "parent-messages-messages"):
            with self.subTest(route=name):
                self.assertEqual(
                    self.get(self.eze.user, name, pk=self.family.pk).status_code, 404,
                )

    def test_another_schools_conversation_answers_like_one_that_does_not_exist(self):
        elsewhere, _, _ = conversations.start_family_from_staff(
            self.solo.tenant, self.ada, kind="FAMILY", student_id=self.bola.pk,
            guardian_id=self.bayo.pk, body="Hello",
        )
        for name in ("parent-messages-conversation", "parent-messages-messages"):
            with self.subTest(route=name):
                there = self.get(self.mother.user, name, pk=elsewhere.pk)
                missing = self.get(self.mother.user, name, pk=MISSING)
                self.assertEqual(there.status_code, 404)
                self.assertEqual(there.data, missing.data)
        read = self.post(self.mother.user, "parent-messages-read", pk=elsewhere.pk)
        self.assertEqual(read.status_code, 404)

    def test_removing_the_guardian_link_ends_access_at_once(self):
        StudentGuardian.objects.filter(student=self.tunde, guardian=self.mother).delete()
        self.assertNotIn(self.family.pk, self.rows(self.mother.user)[0])
        self.assertEqual(self.get(self.mother.user, "parent-messages-conversation", pk=self.family.pk).status_code, 404)
        self.assertEqual(
            self.post(self.mother.user, "parent-messages-messages", {"body": "Hi"}, pk=self.family.pk).status_code,
            404,
        )

    def test_unsetting_the_guardians_account_ends_access_at_once(self):
        account = self.mother.user
        self.mother.user = None
        self.mother.save(update_fields=["user"])
        self.assertEqual(self.get(account, "parent-messages-conversations").status_code, 404)

    def test_a_child_off_the_roll_ends_access(self):
        self.tunde.status = "WITHDRAWN"
        self.tunde.save(update_fields=["status"])
        self.assertNotIn(self.family.pk, self.rows(self.mother.user)[0])
        self.assertEqual(self.get(self.mother.user, "parent-messages-conversation", pk=self.family.pk).status_code, 404)

    def test_a_member_of_staff_who_left_makes_it_read_only(self):
        from schools.vs_staff.models import StaffProfile

        StaffProfile.objects.filter(user=self.funke).update(employment_status="RESIGNED")
        row = self.rows(self.mother.user)[0][self.family.pk]
        self.assertFalse(row["can_send"])
        self.assertEqual(row["read_only_reason"], "No longer at the school.")
        self.assertEqual(
            self.post(self.mother.user, "parent-messages-messages", {"body": "Hi"}, pk=self.family.pk).status_code,
            409,
        )


class ReadingAndWritingTests(ParentMessagesFixture):
    """Messages: paging, sending, retrying and reading."""

    def test_a_parent_reads_the_messages_newest_first(self):
        data = self.results(self.get(self.mother.user, "parent-messages-messages", pk=self.family.pk))
        self.assertEqual([m["body"] for m in data["results"]], [
            "See you at the parents' meeting", "Thank you for telling me", "Tunde did well in the test",
        ])
        self.assertEqual(set(data["results"][0]), {"id", "body", "sender", "sent_at", "mine"})
        self.assertEqual(set(data["results"][0]["sender"]), {"user_id", "name"})
        self.assertFalse(data["has_more"])
        page = self.results(self.get(
            self.mother.user, "parent-messages-messages", {"limit": 1}, pk=self.family.pk,
        ))
        self.assertTrue(page["has_more"])
        older = self.results(self.get(
            self.mother.user, "parent-messages-messages",
            {"before": page["next_before"], "limit": 1}, pk=self.family.pk,
        ))
        self.assertEqual(older["results"][0]["body"], "Thank you for telling me")

    def test_a_parent_writes_and_a_retry_stores_nothing_new(self):
        token = str(uuid.uuid4())
        body = {"body": "Can we meet on Friday?", "client_token": token}
        first = self.post(self.mother.user, "parent-messages-messages", body, pk=self.family.pk)
        again = self.post(self.mother.user, "parent-messages-messages", body, pk=self.family.pk)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(self.results(first)["id"], self.results(again)["id"])
        self.assertTrue(self.results(first)["mine"])
        self.assertEqual(Message.objects.filter(client_token=token).count(), 1)
        staff_view = self.results(self.get(self.funke, "messages-messages", pk=self.family.pk))
        self.assertEqual(staff_view["results"][0]["body"], "Can we meet on Friday?")

    def test_an_empty_message_is_refused(self):
        response = self.post(self.mother.user, "parent-messages-messages", {"body": "  "}, pk=self.family.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "MESSAGE_BODY_REQUIRED")

    def test_reading_clears_the_unread_count(self):
        response = self.post(self.mother.user, "parent-messages-read", pk=self.family.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.results(response), {"conversation": self.family.pk, "unread_count": 0})
        self.assertEqual(self.rows(self.mother.user)[0][self.family.pk]["unread_count"], 0)
        self.assertEqual(self.rows(self.mother.user)[1].data["unread_count"], 1)

    def test_a_watched_conversation_cannot_be_written_in(self):
        response = self.post(self.father.user, "parent-messages-messages", {"body": "Hello"}, pk=self.pupil.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CONVERSATION_READ_ONLY")
        self.assertEqual(Message.objects.filter(conversation=self.pupil).count(), 2)

    def test_a_watched_conversation_can_be_read_by_its_primary_guardian(self):
        data = self.results(self.get(self.father.user, "parent-messages-messages", pk=self.pupil.pk))
        self.assertEqual([m["body"] for m in data["results"]], ["Yes madam", "Please see me about the quiz"])
        detail = self.results(self.get(self.father.user, "parent-messages-conversation", pk=self.pupil.pk))
        self.assertTrue(detail["read_only"])

    def test_opening_a_watched_conversation_changes_nothing_for_anyone(self):
        before = self.snapshot()
        pupil_unread = self.results(self.get(self.funke, "messages-conversation", pk=self.pupil.pk))["unread_count"]
        response = self.post(self.father.user, "parent-messages-read", pk=self.pupil.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.results(response)["unread_count"], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.results(self.get(self.funke, "messages-conversation", pk=self.pupil.pk))["unread_count"],
            pupil_unread,
        )

    def test_a_guardian_who_is_not_primary_cannot_open_the_pupil_conversation(self):
        self.assertEqual(
            self.get(self.mother.user, "parent-messages-messages", pk=self.pupil.pk).status_code, 404,
        )


class ContactsTests(ParentMessagesFixture):
    """Who a parent may write to about a child."""

    def contacts(self, user, **params):
        return self.get(user, "parent-messages-contacts", params)

    def test_the_staff_who_answer_for_a_child_come_with_labels(self):
        response = self.contacts(self.mother.user, child=self.tunde.pk)
        self.assertEqual(response.status_code, 200)
        rows = self.results(response)
        # The class teacher first, the school office (every holder of the office key) last.
        self.assertEqual([r["label"] for r in rows[:2]], ["Class teacher, JSS1 B", "Mathematics"])
        self.assertEqual([r["name"] for r in rows[:2]], ["Funke Adeyemi", "Ibrahim Musa"])
        self.assertEqual({r["label"] for r in rows[2:]}, {"School office"})
        self.assertIn("Ngozi Okoro", [r["name"] for r in rows[2:]])
        self.assertEqual(set(rows[0]), {"user_id", "name", "label"})
        self.assertTrue(response.data["can_start"])
        self.assertEqual(response.data["can_start_reason"], "")

    def test_a_different_child_has_different_staff(self):
        rows = self.results(self.contacts(self.mother.user, child=self.kemi.pk))
        self.assertEqual(rows[0]["name"], "Tola Bankole")
        names = [r["name"] for r in rows]
        self.assertIn("Segun Ola", names)
        self.assertNotIn("Funke Adeyemi", names)

    def test_a_child_that_is_not_theirs_is_a_404_and_no_child_is_a_400(self):
        self.assertEqual(self.contacts(self.mother.user, child=self.chiamaka.pk).status_code, 404)
        self.assertEqual(self.contacts(self.mother.user, child=MISSING).status_code, 404)
        self.assertEqual(self.contacts(self.mother.user).status_code, 400)
        self.assertEqual(self.contacts(self.mother.user, child="x").status_code, 400)

    def test_the_subject_teachers_follow_the_school_setting(self):
        switch(self, "messages.family_reach_subject_teachers", False)
        rows = self.results(self.contacts(self.mother.user, child=self.tunde.pk))
        self.assertEqual(rows[0]["name"], "Funke Adeyemi")
        self.assertNotIn("Ibrahim Musa", [r["name"] for r in rows])

    def test_the_school_switch_is_reported(self):
        switch(self, "messages.families_enabled", False)
        response = self.contacts(self.mother.user, child=self.tunde.pk)
        self.assertFalse(response.data["can_start"])
        self.assertEqual(
            response.data["can_start_reason"], "Messaging with parents is switched off at this school.",
        )


class StartTests(ParentMessagesFixture):
    """A parent starting a conversation."""

    def test_a_parent_starts_a_conversation_and_a_second_tap_finds_it(self):
        body = {"child": self.tunde.pk, "staff": self.ibrahim.pk, "body": "Is Tunde keeping up in maths?"}
        first = self.start(self.mother.user, **body)
        self.assertEqual(first.status_code, 201, first.data)
        data = self.results(first)
        self.assertEqual(data["conversation"]["kind"], "FAMILY")
        self.assertEqual(data["conversation"]["counterpart"]["label"], "Mathematics")
        self.assertEqual(data["message"]["body"], "Is Tunde keeping up in maths?")
        second = self.start(self.mother.user, **{**body, "body": "Any news?"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.results(second)["conversation"]["id"], data["conversation"]["id"])
        self.assertEqual(Conversation.objects.filter(kind="FAMILY", guardian=self.mother).count(), 3)

    def test_the_same_token_stores_the_first_message_once(self):
        body = {
            "child": self.tunde.pk, "staff": self.ibrahim.pk, "body": "Hello sir",
            "client_token": str(uuid.uuid4()),
        }
        self.start(self.mother.user, **body)
        again = self.start(self.mother.user, **body)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(Message.objects.filter(body="Hello sir").count(), 1)

    def test_a_child_that_is_not_theirs_is_a_404(self):
        response = self.start(self.mother.user, child=self.chiamaka.pk, staff=self.ngozi.pk, body="Hi")
        self.assertEqual(response.status_code, 404)

    def test_staff_the_family_may_not_reach_are_refused(self):
        response = self.start(self.mother.user, child=self.tunde.pk, staff=self.tola.pk, body="Hi")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "MESSAGE_RECIPIENT_UNAVAILABLE")
        absent = self.start(self.mother.user, child=self.tunde.pk, staff=MISSING, body="Hi")
        self.assertEqual(absent.data, response.data)

    def test_missing_fields_are_refused(self):
        self.assertEqual(self.start(self.mother.user, staff=self.ngozi.pk).status_code, 400)
        self.assertEqual(self.start(self.mother.user, child=self.tunde.pk).status_code, 400)

    def test_a_school_that_switched_parent_messaging_off_refuses_a_start(self):
        switch(self, "messages.families_enabled", False)
        response = self.start(self.mother.user, child=self.tunde.pk, staff=self.ibrahim.pk, body="Hi")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CONVERSATION_READ_ONLY")
        self.assertEqual(response.data["message"], "Messaging with parents is switched off at this school.")

    def test_existing_conversations_stay_readable_when_it_is_switched_off(self):
        switch(self, "messages.families_enabled", False)
        row = self.rows(self.mother.user)[0][self.family.pk]
        self.assertFalse(row["can_send"])
        self.assertEqual(row["read_only_reason"], "Messaging with parents is switched off at this school.")
        self.assertEqual(
            self.get(self.mother.user, "parent-messages-messages", pk=self.family.pk).status_code, 200,
        )
        sent = self.post(self.mother.user, "parent-messages-messages", {"body": "Hi"}, pk=self.family.pk)
        self.assertEqual(sent.status_code, 409)

    def test_the_pupil_switch_does_not_touch_parents(self):
        switch(self, "messages.pupils_enabled", False)
        self.assertTrue(self.rows(self.mother.user)[0][self.family.pk]["can_send"])


class StaffWhoIsAlsoAParentTests(ParentMessagesFixture):
    """A teacher's staff switches never reach what she reads as a parent."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_rbac.tests.helpers import install_declared_fields

        install_declared_fields("school.messages")

    def test_a_hidden_staff_field_does_not_blank_the_parent_payload(self):
        from vs_rbac.tests.helpers import set_field_access

        # Mrs. Bello's own account is given the teacher role, with names and messages closed.
        from vs_rbac.tests.helpers import make_assignment

        make_assignment(self.school, self.mother.user, self.teacher_role, branch=self.lekki)
        for name in ("participants", "body", "child"):
            set_field_access(self.teacher_role, f"school.messages.{name}", read=False, write=False)
        row = self.rows(self.mother.user)[0][self.family.pk]
        self.assertEqual(row["display_name"], "Funke Adeyemi")
        self.assertEqual(row["counterpart"]["name"], "Funke Adeyemi")
        self.assertEqual(row["child"]["name"], "Tunde Bello")
        self.assertEqual(row["last_message"]["preview"], "See you at the parents' meeting")
        messages = self.results(self.get(self.mother.user, "parent-messages-messages", pk=self.family.pk))
        self.assertEqual(messages["results"][0]["body"], "See you at the parents' meeting")
