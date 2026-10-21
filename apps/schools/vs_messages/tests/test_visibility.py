"""The visibility service: who is current, and what each surface may read.

Access is re-derived on every call, so each test changes one link (an account,
an employment record, a guardian link, a child's status) and reads again.
"""
from __future__ import annotations

from schools.vs_messages.exceptions import ConversationNotFound
from schools.vs_messages.models import Participant
from schools.vs_messages.services import messages, visibility
from schools.vs_staff.constants import EmploymentStatus
from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Guardian, Student, StudentGuardian
from vs_user.models import User

from .base import MessagesFixture


class StaffVisibilityTests(MessagesFixture):
    """Staff access follows employment and the account."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "Can you cover period 3?")

    def ids(self, user):
        return set(visibility.staff_conversations(self.tenant, user).values_list("pk", flat=True))

    def test_only_the_two_participants_read_a_direct_conversation(self):
        self.assertEqual(self.ids(self.funke), {self.dm.pk})
        self.assertEqual(self.ids(self.ibrahim), {self.dm.pk})
        for outsider in (self.tola, self.admin, self.lekki_head, self.ngozi):
            self.assertEqual(self.ids(outsider), set(), outsider.email)
            with self.assertRaises(ConversationNotFound):
                self.access(outsider, self.dm)

    def test_a_member_of_staff_who_leaves_the_roll_loses_the_conversation_at_once(self):
        profile = self.funke.staff_profile
        profile.employment_status = EmploymentStatus.RESIGNED
        profile.save(update_fields=["employment_status"])
        self.assertEqual(self.ids(self.funke), set())
        with self.assertRaises(ConversationNotFound):
            self.access(self.funke, self.dm)

    def test_a_suspended_member_of_staff_is_still_on_the_roll(self):
        profile = self.funke.staff_profile
        profile.employment_status = EmploymentStatus.SUSPENDED
        profile.save(update_fields=["employment_status"])
        self.assertEqual(self.ids(self.funke), {self.dm.pk})

    def test_a_deactivated_account_loses_the_conversation(self):
        User.objects.filter(pk=self.funke.pk).update(is_active=False)
        self.assertEqual(self.ids(self.funke), set())

    def test_an_account_without_a_staff_record_takes_part_in_nothing(self):
        Participant.objects.create(
            tenant=self.tenant, conversation=self.dm, user=self.nobody, role="MEMBER",
        )
        self.assertEqual(self.ids(self.nobody), set())

    def test_the_other_schools_conversations_are_never_visible(self):
        with self.assertRaises(ConversationNotFound):
            visibility.resolve(self.solo.tenant, self.ada, self.dm.pk)
        with self.assertRaises(ConversationNotFound):
            visibility.resolve(self.solo.tenant, self.ada, 999999)

    def test_a_participant_who_left_loses_it(self):
        from django.utils import timezone

        Participant.objects.filter(conversation=self.dm, user=self.ibrahim).update(
            left_at=timezone.now(),
        )
        self.assertEqual(self.ids(self.ibrahim), set())

    def test_a_guardian_participation_is_not_a_staff_conversation(self):
        pair = self.family_chat(self.funke)
        self.assertNotIn(pair.pk, self.ids(self.mother.user))
        self.assertIn(pair.pk, self.ids(self.funke))


class UnreadTests(MessagesFixture):
    """Unread counts and the read watermark."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "Hello")
        cls.say(cls.dm, cls.ibrahim, "Are you there?")
        cls.say(cls.dm, cls.ibrahim, "Please reply")
        cls.group_ = cls.group(cls.funke, [cls.ibrahim], body="Welcome")
        cls.fam = cls.family_chat(cls.funke)
        cls.say(cls.fam, cls.mother.user, "Hello teacher")

    def test_counts_by_kind_exclude_the_callers_own_messages(self):
        funke = visibility.unread_counts(self.tenant, self.funke)
        self.assertEqual(funke["direct"], 2)
        self.assertEqual(funke["group"], 0)
        self.assertEqual(funke["family"], 1)
        self.assertEqual(funke["total"], 3)
        ibrahim = visibility.unread_counts(self.tenant, self.ibrahim)
        # Writing counts as having read everything before it.
        self.assertEqual(ibrahim["direct"], 0)
        self.assertEqual(ibrahim["group"], 1)
        self.assertEqual(ibrahim["total"], 1)

    def test_counting_costs_one_query(self):
        with self.assertNumQueries(1):
            visibility.unread_counts(self.tenant, self.funke)

    def test_a_caller_with_nothing_gets_zeros(self):
        self.assertEqual(
            visibility.unread_counts(self.tenant, self.tola),
            {"direct": 0, "family": 0, "group": 0, "broadcast": 0, "total": 0},
        )

    def test_reading_clears_the_count_and_never_goes_backwards(self):
        access = self.access(self.funke, self.dm)
        newest = visibility.visible_messages(access).order_by("-id").first()
        self.assertEqual(messages.mark_read(access, self.funke), 0)
        self.assertEqual(visibility.unread_counts(self.tenant, self.funke)["direct"], 0)
        # An older "up_to" afterwards leaves the watermark where it was.
        first = visibility.visible_messages(access).order_by("id").first()
        messages.mark_read(access, self.funke, up_to=first.pk)
        mine = Participant.objects.get(conversation=self.dm, user=self.funke)
        self.assertEqual(mine.last_read_message_id, newest.pk)

    def test_up_to_marks_only_that_far(self):
        access = self.access(self.funke, self.dm)
        messages_ = list(visibility.visible_messages(access).order_by("id"))
        # [funke's hello, ibrahim 1, ibrahim 2]
        self.assertEqual(messages.mark_read(access, self.funke, up_to=messages_[1].pk), 1)

    def test_up_to_beyond_the_newest_is_clamped(self):
        access = self.access(self.funke, self.dm)
        self.assertEqual(messages.mark_read(access, self.funke, up_to=10**9), 0)
        mine = Participant.objects.get(conversation=self.dm, user=self.funke)
        self.assertLess(mine.last_read_message_id, 10**9)


class FamilyVisibilityTests(MessagesFixture):
    """Guardian and pupil access follows the links."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.fam = cls.family_chat(cls.funke, cls.tunde, cls.mother, "Tunde did well")
        cls.pup = cls.pupil_chat(cls.funke, cls.tunde, "Please see me")

    def parent_ids(self, user, **kw):
        return set(
            visibility.guardian_conversations(self.tenant, user, **kw).values_list("pk", flat=True)
        )

    def pupil_ids(self, user):
        return set(visibility.pupil_conversations(self.tenant, user).values_list("pk", flat=True))

    def test_the_mother_reads_her_family_conversation_and_not_the_pupils(self):
        self.assertEqual(self.parent_ids(self.mother.user), {self.fam.pk})

    def test_the_primary_guardian_reads_the_pupil_conversation_read_only(self):
        self.assertEqual(self.parent_ids(self.father.user), {self.pup.pk})
        access = self.access(self.father.user, self.pup, surface="parent")
        self.assertFalse(access.can_send)
        self.assertTrue(access.read_only)
        self.assertEqual(access.mode, visibility.MODE_WATCHING)
        self.assertIn("Tunde's conversation", access.reason)
        self.assertIn("read it but not reply", access.reason)

    def test_the_primary_guardian_does_not_read_the_mothers_family_conversation(self):
        with self.assertRaises(ConversationNotFound):
            self.access(self.father.user, self.fam, surface="parent")

    def test_a_stranger_guardian_reads_nothing(self):
        self.assertEqual(self.parent_ids(self.eze.user), set())

    def test_removing_the_link_ends_the_guardians_access(self):
        StudentGuardian.objects.filter(student=self.tunde, guardian=self.mother).delete()
        self.assertEqual(self.parent_ids(self.mother.user), set())
        with self.assertRaises(ConversationNotFound):
            self.access(self.mother.user, self.fam, surface="parent")

    def test_unsetting_the_guardians_account_ends_access(self):
        Guardian.objects.filter(pk=self.mother.pk).update(user=None)
        self.assertEqual(self.parent_ids(self.mother.user), set())

    def test_the_child_leaving_the_roll_ends_every_family_surface(self):
        Student.objects.filter(pk=self.tunde.pk).update(status=StudentStatus.WITHDRAWN)
        self.assertEqual(self.parent_ids(self.mother.user), set())
        self.assertEqual(self.parent_ids(self.father.user), set())
        self.assertEqual(self.pupil_ids(self.tunde.user), set())

    def test_changing_the_primary_guardian_moves_the_pupil_view(self):
        StudentGuardian.objects.filter(student=self.tunde).update(is_primary=False)
        StudentGuardian.objects.filter(student=self.tunde, guardian=self.mother).update(
            is_primary=True,
        )
        self.assertEqual(self.parent_ids(self.father.user), set())
        self.assertEqual(self.parent_ids(self.mother.user), {self.fam.pk, self.pup.pk})

    def test_child_narrows_the_parents_list(self):
        self.assertEqual(self.parent_ids(self.mother.user, child=self.tunde), {self.fam.pk})
        self.assertEqual(self.parent_ids(self.mother.user, child=self.chiamaka), set())

    def test_the_pupil_reads_their_own_conversation_only(self):
        self.assertEqual(self.pupil_ids(self.tunde.user), {self.pup.pk})
        self.assertEqual(self.pupil_ids(self.chiamaka.user), set())
        access = self.access(self.tunde.user, self.pup, surface="student")
        self.assertTrue(access.can_send)
        with self.assertRaises(ConversationNotFound):
            self.access(self.tunde.user, self.fam, surface="student")

    def test_unlinking_the_pupil_account_ends_their_access(self):
        Student.objects.filter(pk=self.tunde.pk).update(user=None)
        self.assertEqual(self.pupil_ids(self.tunde.user), set())

    def test_the_family_sees_it_read_only_when_the_member_of_staff_has_left(self):
        profile = self.funke.staff_profile
        profile.employment_status = EmploymentStatus.RESIGNED
        profile.save(update_fields=["employment_status"])
        for user, surface, convo in (
            (self.mother.user, "parent", self.fam), (self.tunde.user, "student", self.pup),
        ):
            access = self.access(user, convo, surface=surface)
            self.assertFalse(access.can_send)
            self.assertEqual(access.reason, "No longer at the school.")
            # Still readable.
            self.assertEqual(visibility.visible_messages(access).count(), 1)

    def test_switching_families_off_makes_existing_conversations_read_only(self):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        set_value(
            definition=ConfigurationDefinition.objects.get(key="messages.families_enabled"),
            value=False, actor=self.admin, tenant=self.tenant,
        )
        parent = self.access(self.mother.user, self.fam, surface="parent")
        self.assertFalse(parent.can_send)
        self.assertIn("switched off", parent.reason)
        # Pupils are a separate switch.
        self.assertTrue(self.access(self.tunde.user, self.pup, surface="student").can_send)

    def test_a_staff_participation_of_a_family_conversation_belongs_to_the_staff_surface(self):
        staff = visibility.staff_conversations(self.tenant, self.funke)
        self.assertEqual(set(staff.values_list("pk", flat=True)), {self.fam.pk, self.pup.pk})

    def test_unread_for_a_parent_counts_family_messages_only(self):
        self.say(self.fam, self.funke, "Second note")
        self.say(self.pup, self.funke, "Pupil note")
        self.assertEqual(visibility.unread_counts(self.tenant, self.mother.user, surface="parent"),
                         {"total": 1 + 1})  # first note + second note
        # The primary guardian watches the pupil conversation and is never counted.
        self.assertEqual(visibility.unread_counts(self.tenant, self.father.user, surface="parent"),
                         {"total": 0})
        self.assertEqual(visibility.unread_counts(self.tenant, self.tunde.user, surface="student"),
                         {"total": 2})


class AlertRecipientsTests(MessagesFixture):
    """Who is alerted about a new message."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dm = cls.direct(cls.funke, cls.ibrahim, "Hello")

    def recipients(self, message):
        return {p.user_id for p in visibility.alert_recipients(message)}

    def test_the_first_unread_message_alerts_the_other_end_only(self):
        message = self.say(self.dm, self.funke, "Second")
        # ibrahim already has "Hello" unread, so a second message adds no alert.
        self.assertEqual(self.recipients(message), set())

    def test_a_message_after_the_conversation_was_read_alerts_again(self):
        access = self.access(self.ibrahim, self.dm)
        messages.mark_read(access, self.ibrahim)
        message = self.say(self.dm, self.funke, "Next")
        self.assertEqual(self.recipients(message), {self.ibrahim.pk})

    def test_a_participant_who_is_no_longer_current_is_not_alerted(self):
        access = self.access(self.ibrahim, self.dm)
        messages.mark_read(access, self.ibrahim)
        profile = self.ibrahim.staff_profile
        profile.employment_status = EmploymentStatus.RESIGNED
        profile.save(update_fields=["employment_status"])
        self.assertEqual(self.recipients(self.say(self.dm, self.funke, "Next")), set())

    def test_the_family_is_alerted_while_the_link_stands_and_not_after(self):
        fam = self.family_chat(self.funke, body="Hello")
        messages.mark_read(self.access(self.mother.user, fam, "parent"), self.mother.user)
        first = self.say(fam, self.funke, "Again")
        self.assertEqual(self.recipients(first), {self.mother.user_id})
        Participant.objects.filter(conversation=fam, user=self.mother.user).update(
            last_read_message_id=first.pk,
        )
        StudentGuardian.objects.filter(student=self.tunde, guardian=self.mother).delete()
        self.assertNotIn(self.mother.user_id, self.recipients(self.say(fam, self.funke, "Once more")))

    def test_the_primary_guardian_watching_a_pupil_conversation_is_never_alerted(self):
        pup = self.pupil_chat(self.funke, body="Hello")
        access = self.access(self.tunde.user, pup, surface="student")
        messages.mark_read(access, self.tunde.user)
        message = self.say(pup, self.funke, "Next")
        self.assertEqual(self.recipients(message), {self.tunde.user_id})
        self.assertNotIn(self.father.user_id, self.recipients(message))
