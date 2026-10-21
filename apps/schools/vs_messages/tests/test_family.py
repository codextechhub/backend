"""Parent and pupil conversations: who can reach whom, from either end.

Reach follows the child's class teacher this year, the teachers of the child's
class (a school setting), and the branch's school office. Tunde is in JSS1 B at
Lekki, which Mrs. Adeyemi leads and Mr. Musa teaches; Mrs. Okoro is the Lekki
office and Mr. Ola the Ikeja one.
"""
from __future__ import annotations

from schools.vs_messages.exceptions import ConversationReadOnly, RecipientUnavailable
from schools.vs_messages.models import Conversation
from schools.vs_messages.services import config, conversations, messages, people, visibility
from schools.vs_students.models import Guardian, Student
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import MessagesFixture

MISSING = 999999


def switch(test, key, value, tenant=None):
    set_value(
        definition=ConfigurationDefinition.objects.get(key=key), value=value,
        actor=test.admin, tenant=tenant or test.tenant,
    )


class StaffStartsTests(MessagesFixture):
    """Staff starting a parent or pupil conversation."""
    def start(self, user, **body):
        return self.post(user, "messages-conversations", body)

    def family(self, user, student=None, guardian=None, **extra):
        return self.start(
            user, kind="FAMILY", student=(student or self.tunde).pk,
            guardian=(guardian or self.mother).pk, body="Hello", **extra,
        )

    def test_the_class_teacher_starts_a_conversation_with_a_guardian(self):
        response = self.family(self.funke)
        self.assertEqual(response.status_code, 201, response.data)
        data = self.results(response)["conversation"]
        self.assertEqual(data["kind"], "FAMILY")
        self.assertEqual(data["counterpart"]["name"], "Funke Bello")
        self.assertEqual(data["counterpart"]["label"], "Mother of Tunde Bello")
        self.assertEqual(data["child"]["name"], "Tunde Bello")
        self.assertEqual(data["child"]["class_name"], "JSS1 B")
        self.assertEqual(self.family(self.funke).status_code, 200)
        self.assertEqual(Conversation.objects.filter(kind="FAMILY").count(), 1)

    def test_a_pupil_conversation_names_no_guardian(self):
        response = self.start(self.funke, kind="PUPIL", student=self.tunde.pk, body="Hi Tunde")
        self.assertEqual(response.status_code, 201, response.data)
        row = self.results(response)["conversation"]
        self.assertEqual(row["counterpart"], {
            "user_id": self.tunde.user_id, "name": "Tunde Bello", "label": "Pupil",
        })

    def test_a_subject_teacher_reaches_the_class_while_the_school_lets_them(self):
        self.assertEqual(self.family(self.ibrahim).status_code, 201)
        switch(self, "messages.family_reach_subject_teachers", False)
        self.assertEqual(self.family(self.ibrahim, guardian=self.father).status_code, 400)
        # The class teacher is unaffected.
        self.assertEqual(self.family(self.funke).status_code, 201)

    def test_the_school_office_reaches_children_at_its_own_branch_only(self):
        self.assertEqual(self.family(self.ngozi).status_code, 201)
        self.assertEqual(
            self.family(self.ngozi, student=self.chiamaka, guardian=self.eze).status_code, 201,
        )
        refused = self.start(
            self.ngozi, kind="PUPIL", student=self.kemi.pk, body="Hi",
        )
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.data["error"]["code"], "MESSAGE_RECIPIENT_UNAVAILABLE")
        self.assertEqual(
            self.start(self.segun, kind="PUPIL", student=self.kemi.pk, body="Hi").status_code, 201,
        )

    def test_a_teacher_cannot_reach_a_child_outside_their_class(self):
        outside = self.start(self.funke, kind="PUPIL", student=self.kemi.pk, body="Hi")
        missing = self.start(self.funke, kind="PUPIL", student=MISSING, body="Hi")
        self.assertEqual(outside.status_code, 400)
        self.assertEqual(outside.data, missing.data)

    def test_a_guardian_who_is_not_the_childs_is_refused_the_same_way(self):
        response = self.family(self.funke, guardian=self.eze)
        self.assertEqual(response.status_code, 400)

    def test_a_guardian_without_an_account_cannot_be_started_with(self):
        Guardian.objects.filter(pk=self.mother.pk).update(user=None)
        self.assertEqual(self.family(self.funke).status_code, 400)

    def test_a_pupil_without_an_account_cannot_be_started_with(self):
        Student.objects.filter(pk=self.tunde.pk).update(user=None)
        self.assertEqual(
            self.start(self.funke, kind="PUPIL", student=self.tunde.pk).status_code, 400,
        )

    def test_a_reader_without_the_family_key_is_refused(self):
        self.assertEqual(self.family(self.reader).status_code, 403)

    def test_switching_families_off_stops_new_conversations(self):
        switch(self, "messages.families_enabled", False)
        response = self.family(self.funke)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CONVERSATION_READ_ONLY")
        # Pupils are a separate switch.
        self.assertEqual(
            self.start(self.funke, kind="PUPIL", student=self.tunde.pk, body="Hi").status_code, 201,
        )

    def test_a_child_who_left_the_roll_cannot_be_started_with(self):
        from schools.vs_students.constants import StudentStatus

        Student.objects.filter(pk=self.tunde.pk).update(status=StudentStatus.WITHDRAWN)
        self.assertEqual(self.family(self.funke).status_code, 400)

    def test_a_staff_kind_start_cannot_carry_a_child(self):
        response = self.start(self.funke, kind="STAFF_DIRECT", recipient=self.ibrahim.pk, student=self.tunde.pk)
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(self.results(response)["conversation"]["child"])


class FamilyContactTests(MessagesFixture):
    """The pupils and guardians a member of staff may reach."""
    def rows(self, user, **params):
        return self.results(self.get(user, "messages-contacts", {"kind": "family", **params}))

    def test_a_class_teacher_sees_their_class_with_the_guardians(self):
        rows = self.rows(self.funke)
        self.assertEqual([r["student"]["name"] for r in rows], ["Tunde Bello"])
        row = rows[0]
        self.assertEqual(row["student"]["class_name"], "JSS1 B")
        self.assertTrue(row["pupil_has_account"])
        names = {g["name"]: g for g in row["guardians"]}
        self.assertEqual(names["Funke Bello"]["relationship"], "Mother")
        self.assertTrue(names["Funke Bello"]["has_account"])

    def test_the_office_sees_the_children_of_its_branch(self):
        self.assertEqual({r["student"]["name"] for r in self.rows(self.ngozi)},
                         {"Tunde Bello", "Chiamaka Eze"})
        self.assertEqual({r["student"]["name"] for r in self.rows(self.segun)}, {"Kemi Pupil"})

    def test_the_whole_school_office_sees_every_child(self):
        self.assertEqual(len(self.rows(self.admin)), 3)

    def test_search_by_pupil_name(self):
        self.assertEqual([r["student"]["name"] for r in self.rows(self.admin, search="chiam")],
                         ["Chiamaka Eze"])

    def test_a_guardian_with_no_account_is_marked(self):
        Guardian.objects.filter(pk=self.mother.pk).update(user=None)
        guardians = {g["name"]: g["has_account"] for g in self.rows(self.funke)[0]["guardians"]}
        self.assertFalse(guardians["Funke Bello"])
        self.assertTrue(guardians["Kunle Bello"])

    def test_a_caller_with_neither_key_is_refused_and_a_teacher_cannot_list_staff(self):
        self.assertEqual(self.get(self.nobody, "messages-contacts", {"kind": "family"}).status_code, 403)
        self.assertEqual(self.get(self.reader, "messages-contacts", {"kind": "family"}).status_code, 403)

    def test_the_office_key_without_a_branch_covering_it_reaches_nothing(self):
        """Mr. Ola's office cover is Ikeja; a Lekki child is not his."""
        self.assertNotIn("Tunde Bello", {r["student"]["name"] for r in self.rows(self.segun)})


class GuardianStartsTests(MessagesFixture):
    """A guardian starting a conversation with the staff they may reach."""
    def reachable(self, child=None):
        return people.staff_reachable_for_child(
            self.tenant, child or self.tunde, config.load_settings(self.tenant),
        )

    def test_the_reachable_staff_and_their_labels(self):
        reach = self.reachable()
        self.assertEqual(reach[self.funke.pk], "Class teacher, JSS1 B")
        self.assertEqual(reach[self.ibrahim.pk], "Mathematics")
        self.assertEqual(reach[self.ngozi.pk], "School office")
        # The whole-school administrator and the Lekki head also hold the office key.
        self.assertEqual(reach[self.admin.pk], "School office")
        self.assertEqual(reach[self.lekki_head.pk], "School office")
        for left_out in (self.tola, self.segun, self.reader):
            self.assertNotIn(left_out.pk, reach)

    def test_without_subject_teachers_only_the_class_teacher_and_the_office_remain(self):
        switch(self, "messages.family_reach_subject_teachers", False)
        reach = self.reachable()
        self.assertIn(self.funke.pk, reach)
        self.assertNotIn(self.ibrahim.pk, reach)
        self.assertIn(self.ngozi.pk, reach)

    def test_a_guardian_starts_with_the_class_teacher(self):
        conversation, message, created = conversations.start_from_guardian(
            self.tenant, self.mother.user, student_id=self.tunde.pk,
            staff_user_id=self.funke.pk, body="Tunde is unwell",
        )
        self.assertTrue(created)
        self.assertEqual(conversation.kind, "FAMILY")
        self.assertEqual(message.body, "Tunde is unwell")
        self.assertEqual(
            conversation.pair_key, f"family:{self.mother.pk}:{self.funke.pk}:{self.tunde.pk}",
        )
        again, _, created = conversations.start_from_guardian(
            self.tenant, self.mother.user, student_id=self.tunde.pk, staff_user_id=self.funke.pk,
        )
        self.assertFalse(created)
        self.assertEqual(again.pk, conversation.pk)
        # The member of staff finds it in their inbox.
        row = self.results(self.get(self.funke, "messages-conversations"))[0]
        self.assertEqual(row["counterpart"]["name"], "Funke Bello")
        self.assertEqual(row["unread_count"], 1)

    def test_staff_outside_the_reach_are_refused_like_staff_who_do_not_exist(self):
        for staff_id in (self.tola.pk, self.reader.pk, MISSING):
            with self.assertRaises(RecipientUnavailable):
                conversations.start_from_guardian(
                    self.tenant, self.mother.user, student_id=self.tunde.pk, staff_user_id=staff_id,
                )

    def test_another_familys_child_is_refused(self):
        with self.assertRaises(RecipientUnavailable):
            conversations.start_from_guardian(
                self.tenant, self.eze.user, student_id=self.tunde.pk, staff_user_id=self.funke.pk,
            )

    def test_a_guardian_cannot_start_when_families_are_switched_off(self):
        switch(self, "messages.families_enabled", False)
        with self.assertRaises(ConversationReadOnly):
            conversations.start_from_guardian(
                self.tenant, self.mother.user, student_id=self.tunde.pk, staff_user_id=self.funke.pk,
            )

    def test_two_guardians_of_one_child_make_two_conversations_with_one_teacher(self):
        a, _, _ = conversations.start_from_guardian(
            self.tenant, self.mother.user, student_id=self.tunde.pk, staff_user_id=self.funke.pk,
        )
        b, _, _ = conversations.start_from_guardian(
            self.tenant, self.father.user, student_id=self.tunde.pk, staff_user_id=self.funke.pk,
        )
        self.assertNotEqual(a.pk, b.pk)

    def test_the_guardian_writes_and_the_staff_member_reads(self):
        conversation, _, _ = conversations.start_from_guardian(
            self.tenant, self.mother.user, student_id=self.tunde.pk, staff_user_id=self.funke.pk,
            body="Hello",
        )
        access = visibility.resolve(self.tenant, self.mother.user, conversation.pk, surface="parent")
        self.assertTrue(access.can_send)
        messages.send_message(access, sender=self.mother.user, body="Second")
        page = self.results(self.get(self.funke, "messages-messages", pk=conversation.pk))
        self.assertEqual([m["body"] for m in page["results"]], ["Second", "Hello"])
        self.assertEqual(page["results"][0]["sender"]["name"], "Funke Bello")


class PupilStartsTests(MessagesFixture):
    """A pupil starting a conversation with their teachers or the office."""
    def test_a_pupil_starts_with_a_teacher_and_the_office(self):
        for staff in (self.funke, self.ibrahim, self.ngozi):
            conversation, message, created = conversations.start_from_pupil(
                self.tenant, self.tunde.user, staff_user_id=staff.pk, body="Sir, a question",
            )
            self.assertTrue(created)
            self.assertEqual(conversation.kind, "PUPIL")
            self.assertEqual(conversation.pair_key, f"pupil:{self.tunde.pk}:{staff.pk}")

    def test_a_pupil_cannot_reach_staff_of_another_class(self):
        with self.assertRaises(RecipientUnavailable):
            conversations.start_from_pupil(
                self.tenant, self.tunde.user, staff_user_id=self.tola.pk,
            )

    def test_a_user_who_is_not_a_pupil_is_refused(self):
        with self.assertRaises(RecipientUnavailable):
            conversations.start_from_pupil(
                self.tenant, self.funke, staff_user_id=self.ngozi.pk,
            )

    def test_switching_pupils_off_stops_new_conversations(self):
        switch(self, "messages.pupils_enabled", False)
        with self.assertRaises(ConversationReadOnly):
            conversations.start_from_pupil(
                self.tenant, self.tunde.user, staff_user_id=self.funke.pk,
            )


class OneBranchSchoolTests(MessagesFixture):
    """At Sunrise there is one branch, so the dimension recedes."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group_ = groups_create(cls)

    def test_the_family_flow_works_with_one_branch(self):
        conversation, message, _ = conversations.start_from_guardian(
            self.solo.tenant, self.bayo.user, student_id=self.bola.pk, staff_user_id=self.ada.pk,
            body="Hello",
        )
        self.assertEqual(conversation.kind, "FAMILY")
        row = self.results(self.get(self.ada, "messages-conversations"))[0]
        self.assertNotIn("branch", row)

    def test_a_group_row_has_no_branch_at_a_one_branch_school(self):
        row = [r for r in self.results(self.get(self.ada, "messages-conversations"))
               if r["kind"] == "GROUP"][0]
        self.assertNotIn("branch", row)

    def test_a_teacher_pinned_to_the_only_branch_may_make_a_school_wide_group(self):
        """The only branch is the whole school, so the reach covers it all."""
        self.assertIsNone(self.group_.branch_id)


def groups_create(cls):
    from schools.vs_messages.services import groups

    group, _ = groups.create_group(
        cls.solo.tenant, cls.ada, name="Sunrise staff", branch_id=None,
        member_ids=[cls.solo_admin.pk], body="Hello",
    )
    return group
