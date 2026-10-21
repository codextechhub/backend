"""The student app's messages: a pupil's own conversations and who they can write to.

At Brightfield, Tunde Bello (JSS1 B, Lekki) is taught by Mrs. Adeyemi (his
class teacher) and Mr. Musa (Mathematics), and the Lekki school office is Mrs.
Okoro. His father is his primary guardian. Kemi Pupil attends the Ikeja branch.
"""
from __future__ import annotations

import uuid

from schools.vs_messages.models import Message
from schools.vs_messages.services import conversations
from schools.vs_students.models import Student

from .base import MessagesFixture
from .test_family import switch

MISSING = 999999
NOTICE = "Your parent or guardian and the school can read your messages."
ROW_KEYS = {
    "id", "kind", "kind_label", "display_name", "counterpart", "last_message",
    "last_activity_at", "unread_count", "read_only", "can_send", "read_only_reason",
}


class StudentMessagesFixture(MessagesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chat = cls.pupil_chat(cls.funke, cls.tunde, "Please bring your project on Monday")
        cls.say(cls.chat, cls.tunde.user, "Yes madam")
        cls.say(cls.chat, cls.funke, "Thank you")
        cls.kemi_chat = cls.pupil_chat(cls.segun, cls.kemi, "Your locker key is ready")
        cls.mother_chat = cls.family_chat(cls.funke, cls.tunde, cls.mother, "Tunde did well")

    def rows(self, user, **params):
        response = self.get(user, "student-messages-conversations", params)
        self.assertEqual(response.status_code, 200, response.data)
        return {r["id"]: r for r in self.results(response)}, response

    def start(self, user, **body):
        return self.post(user, "student-messages-conversations", body)


class ListTests(StudentMessagesFixture):
    def test_a_pupil_sees_only_their_own_conversations(self):
        rows, response = self.rows(self.tunde.user)
        self.assertEqual(set(rows), {self.chat.pk})
        row = rows[self.chat.pk]
        self.assertEqual(set(row), ROW_KEYS)
        self.assertEqual(row["counterpart"]["name"], "Funke Adeyemi")
        self.assertEqual(row["counterpart"]["label"], "Class teacher, JSS1 B")
        self.assertEqual(row["unread_count"], 1)
        self.assertTrue(row["can_send"])
        self.assertEqual(response.data["unread_count"], 1)

    def test_the_list_says_who_else_can_read(self):
        response = self.rows(self.tunde.user)[1]
        self.assertEqual(response.data["oversight_notice"], NOTICE)
        self.assertEqual(self.rows(self.kemi.user)[1].data["oversight_notice"], NOTICE)

    def test_a_pupil_never_sees_a_parent_conversation_about_themselves(self):
        self.assertNotIn(self.mother_chat.pk, self.rows(self.tunde.user)[0])
        self.assertEqual(
            self.get(self.tunde.user, "student-messages-conversation", pk=self.mother_chat.pk).status_code,
            404,
        )

    def test_search_and_unread(self):
        self.assertEqual(set(self.rows(self.tunde.user, search="Adeyemi")[0]), {self.chat.pk})
        self.assertEqual(self.rows(self.tunde.user, search="Okoro")[0], {})
        self.assertEqual(set(self.rows(self.tunde.user, unread="true")[0]), {self.chat.pk})

    def test_unread_count_route(self):
        self.assertEqual(
            self.results(self.get(self.tunde.user, "student-messages-unread-count")), {"total": 1},
        )


class AccessTests(StudentMessagesFixture):
    def test_someone_who_is_not_a_pupil_is_refused(self):
        for user in (self.funke, self.mother.user):
            with self.subTest(user=user.email):
                self.assertEqual(self.get(user, "student-messages-conversations").status_code, 404)

    def test_another_pupils_conversation_is_a_404(self):
        for name in ("student-messages-conversation", "student-messages-messages"):
            with self.subTest(route=name):
                self.assertEqual(self.get(self.kemi.user, name, pk=self.chat.pk).status_code, 404)

    def test_another_schools_conversation_answers_like_one_that_does_not_exist(self):
        elsewhere, _, _ = conversations.start_family_from_staff(
            self.solo.tenant, self.ada, kind="PUPIL", student_id=self.bola.pk, body="Hello",
        )
        there = self.get(self.tunde.user, "student-messages-conversation", pk=elsewhere.pk)
        missing = self.get(self.tunde.user, "student-messages-conversation", pk=MISSING)
        self.assertEqual(there.status_code, 404)
        self.assertEqual(there.data, missing.data)

    def test_a_pupil_off_the_roll_is_refused(self):
        Student.objects.filter(pk=self.tunde.pk).update(status="WITHDRAWN")
        self.assertEqual(self.get(self.tunde.user, "student-messages-conversations").status_code, 404)

    def test_a_member_of_staff_who_left_makes_it_read_only(self):
        from schools.vs_staff.models import StaffProfile

        StaffProfile.objects.filter(user=self.funke).update(employment_status="RESIGNED")
        row = self.rows(self.tunde.user)[0][self.chat.pk]
        self.assertFalse(row["can_send"])
        self.assertEqual(row["read_only_reason"], "No longer at the school.")


class ReadingAndWritingTests(StudentMessagesFixture):
    def test_a_pupil_reads_and_writes(self):
        data = self.results(self.get(self.tunde.user, "student-messages-messages", pk=self.chat.pk))
        self.assertEqual(
            [m["body"] for m in data["results"]],
            ["Thank you", "Yes madam", "Please bring your project on Monday"],
        )
        token = str(uuid.uuid4())
        body = {"body": "I have it", "client_token": token}
        first = self.post(self.tunde.user, "student-messages-messages", body, pk=self.chat.pk)
        again = self.post(self.tunde.user, "student-messages-messages", body, pk=self.chat.pk)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(Message.objects.filter(client_token=token).count(), 1)

    def test_reading_clears_what_is_unread(self):
        response = self.post(self.tunde.user, "student-messages-read", pk=self.chat.pk)
        self.assertEqual(self.results(response), {"conversation": self.chat.pk, "unread_count": 0})
        self.assertEqual(self.rows(self.tunde.user)[1].data["unread_count"], 0)

    def test_the_pupil_switch_makes_conversations_read_only(self):
        switch(self, "messages.pupils_enabled", False)
        row = self.rows(self.tunde.user)[0][self.chat.pk]
        self.assertFalse(row["can_send"])
        self.assertEqual(row["read_only_reason"], "Messaging with pupils is switched off at this school.")
        sent = self.post(self.tunde.user, "student-messages-messages", {"body": "Hi"}, pk=self.chat.pk)
        self.assertEqual(sent.status_code, 409)
        self.assertEqual(sent.data["error"]["code"], "CONVERSATION_READ_ONLY")
        self.assertEqual(
            self.get(self.tunde.user, "student-messages-messages", pk=self.chat.pk).status_code, 200,
        )

    def test_the_family_switch_does_not_touch_pupils(self):
        switch(self, "messages.families_enabled", False)
        self.assertTrue(self.rows(self.tunde.user)[0][self.chat.pk]["can_send"])


class ContactsAndStartTests(StudentMessagesFixture):
    def test_contacts_are_the_pupils_teachers_and_the_office(self):
        response = self.get(self.tunde.user, "student-messages-contacts")
        rows = self.results(response)
        self.assertEqual([r["label"] for r in rows[:2]], ["Class teacher, JSS1 B", "Mathematics"])
        self.assertEqual([r["name"] for r in rows[:2]], ["Funke Adeyemi", "Ibrahim Musa"])
        self.assertEqual({r["label"] for r in rows[2:]}, {"School office"})
        self.assertIn("Ngozi Okoro", [r["name"] for r in rows[2:]])
        self.assertEqual(set(rows[0]), {"user_id", "name", "label"})
        self.assertEqual(response.data["oversight_notice"], NOTICE)
        self.assertTrue(response.data["can_start"])
        ikeja = self.results(self.get(self.kemi.user, "student-messages-contacts"))
        self.assertEqual(ikeja[0]["name"], "Tola Bankole")
        self.assertNotIn("Funke Adeyemi", [r["name"] for r in ikeja])

    def test_the_pupil_switch_is_reported(self):
        switch(self, "messages.pupils_enabled", False)
        response = self.get(self.tunde.user, "student-messages-contacts")
        self.assertFalse(response.data["can_start"])
        self.assertEqual(
            response.data["can_start_reason"], "Messaging with pupils is switched off at this school.",
        )

    def test_a_pupil_starts_a_conversation_and_a_second_tap_finds_it(self):
        body = {"staff": self.ibrahim.pk, "body": "Sir, I missed the last class."}
        first = self.start(self.tunde.user, **body)
        self.assertEqual(first.status_code, 201, first.data)
        data = self.results(first)
        self.assertEqual(data["conversation"]["kind"], "PUPIL")
        self.assertEqual(data["conversation"]["counterpart"]["label"], "Mathematics")
        self.assertNotIn("child", data["conversation"])
        self.assertEqual(data["message"]["body"], "Sir, I missed the last class.")
        second = self.start(self.tunde.user, staff=self.ibrahim.pk, body="Thank you")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.results(second)["conversation"]["id"], data["conversation"]["id"])

    def test_staff_a_pupil_may_not_reach_are_refused_like_staff_who_do_not_exist(self):
        refused = self.start(self.tunde.user, staff=self.tola.pk, body="Hi")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.data["error"]["code"], "MESSAGE_RECIPIENT_UNAVAILABLE")
        self.assertEqual(self.start(self.tunde.user, staff=MISSING, body="Hi").data, refused.data)

    def test_a_start_is_refused_when_the_school_switched_pupil_messaging_off(self):
        switch(self, "messages.pupils_enabled", False)
        response = self.start(self.tunde.user, staff=self.ibrahim.pk, body="Hi")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CONVERSATION_READ_ONLY")
        self.assertEqual(response.data["message"], "Messaging with pupils is switched off at this school.")
