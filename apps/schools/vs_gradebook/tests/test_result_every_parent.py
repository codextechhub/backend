"""Telling every parent of a released result, where the school chooses it.

At Brightfield (two branches), Chiamaka's mother Mrs. Ngozi Nwosu is her
primary guardian and has an email address only; her father Mr. Emeka Nwosu has a
parent account. Kelechi and Amina share their primary guardian, Mrs. Eze, who has
an account; Kelechi's uncle Mr. Obi has one too, and Amina's aunt's account is
deactivated. At Sunrise (one branch), Mr. Okoro is his child's primary guardian
by email and the child's mother Mrs. Okoro has an account.

A school starts on the primary guardian alone. When it tells every parent, each
child's other parents with an account are told in the app and by push, never by
email, and Mrs. Eze, already told as the primary guardian, is not told twice.
"""
from __future__ import annotations

from unittest import mock

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_gradebook.constants import (
    CFG_TELL_EVERY_PARENT,
    EVENT_REPORT_CARD_READY,
    EVENT_RESULT_RELEASED_PARENT,
    SheetStatus,
)
from schools.vs_gradebook.models import ResultHold
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian, StudentGuardian
from vs_rbac.tests.helpers import make_school_admin
from vs_user.models import User

from .base import set_setting
from .class_base import ClassFixture
from .test_publish import SEND, SUMMARY, counts_for


class EveryParentFixture(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()
        cls.emeka = cls.parent(cls.chiamaka, "emeka.nwosu@example.ng", "Emeka", "Nwosu")
        cls.eze_user = cls.account("mrs.eze@example.ng", "Ngozi", "Eze")
        Guardian.all_objects.filter(pk=cls.eze_mother.pk).update(user=cls.eze_user)
        cls.obi = cls.parent(cls.kelechi, "obi@example.ng", "Chidi", "Obi")
        cls.gone = cls.parent(cls.amina, "aunt.yusuf@example.ng", "Hauwa", "Yusuf")
        cls.gone.status = User.Status.DEACTIVATED
        cls.gone.save()

        cls.mrs_okoro = cls.parent(
            cls.solo_pupil, "mrs.okoro@example.ng", "Bisi", "Okoro", tenant=cls.solo,
        )
        cls.make_sheet(
            cls.solo_class, cls.solo_maths, status=SheetStatus.SUBMITTED,
            rows={cls.solo_pupil: (18, 15, 46), cls.solo_pupil_two: (10, 10, 20)},
        )
        client = TenantAPIClient(cls.solo_teacher)
        for pupil in (cls.solo_pupil, cls.solo_pupil_two):
            client.put(
                reverse("gradebook-compile", kwargs={"class_id": cls.solo_class.pk, "student_id": pupil.pk})
                + f"?term={cls.solo_term.pk}", {"teacher_comment": "Good."}, format="json",
            )
        client.post(
            reverse("gradebook-class-submit", kwargs={"class_id": cls.solo_class.pk})
            + f"?term={cls.solo_term.pk}", {}, format="json",
        )

    @classmethod
    def account(cls, email, first, last, *, tenant=None):
        return make_school_admin(
            None, email=email, first_name=first, last_name=last, tenant=tenant or cls.tenant,
        )

    @classmethod
    def parent(cls, child, email, first, last, *, tenant=None):
        """A parent with an account, linked to *child* but not its primary guardian."""
        tenant = tenant or cls.tenant
        user = cls.account(email, first, last, tenant=tenant)
        guardian = Guardian.all_objects.create(
            tenant=tenant, full_name=f"{first} {last}", email=email, phone="08035550199", user=user,
        )
        StudentGuardian.all_objects.create(
            tenant=tenant, student=child, guardian=guardian,
            relationship=Relationship.OTHER, is_primary=False,
        )
        return user

    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        summary = mock.patch(SUMMARY, return_value=counts_for(
            self.chiamaka, self.tunde, self.kelechi, self.amina,
        ))
        summary.start()
        self.addCleanup(summary.stop)

    def told(self, event_key):
        """``{child's first name: (recipients, metadata)}`` for each send of *event_key*."""
        return {
            call.kwargs["context"]["student_first_name"]: (
                call.kwargs["recipients"], call.kwargs.get("metadata"),
            )
            for call in self.send.call_args_list if call.kwargs["event_key"] == event_key
        }


class PrimaryGuardianAloneTests(EveryParentFixture):
    def test_a_school_that_has_not_chosen_tells_no_other_parent(self):
        self.publish(self.principal)
        self.assertEqual(self.told(EVENT_RESULT_RELEASED_PARENT), {})
        self.assertIn("Chiamaka", self.told(EVENT_REPORT_CARD_READY))


class EveryParentTests(EveryParentFixture):
    def setUp(self):
        super().setUp()
        set_setting(CFG_TELL_EVERY_PARENT, True, self.tenant, self.admin)

    def test_each_childs_other_parents_with_an_account_are_told_naming_the_child(self):
        self.publish(self.principal)
        told = self.told(EVENT_RESULT_RELEASED_PARENT)
        self.assertEqual(told["Chiamaka"], ([self.emeka], {"student_id": self.chiamaka.pk}))
        self.assertEqual(told["Kelechi"], ([self.obi], {"student_id": self.kelechi.pk}))

    def test_the_primary_guardian_already_told_and_a_closed_account_are_not_told(self):
        self.publish(self.principal)
        told = self.told(EVENT_RESULT_RELEASED_PARENT)
        self.assertNotIn("Amina", told)
        self.assertNotIn("Tunde", told)
        for recipients, _ in told.values():
            self.assertNotIn(self.eze_user, recipients)
            self.assertNotIn(self.gone, recipients)
        self.assertEqual(self.told(EVENT_REPORT_CARD_READY)["Kelechi"][0], [self.eze_user])

    def test_the_report_card_email_still_goes_to_the_primary_guardian_alone(self):
        self.publish(self.principal)
        call = next(
            c for c in self.send.call_args_list
            if c.kwargs["event_key"] == EVENT_REPORT_CARD_READY
            and c.kwargs["context"]["student_first_name"] == "Chiamaka"
        )
        [recipient] = call.kwargs["unregistered_recipients"]
        self.assertEqual(recipient.email, "ngozi.nwosu@example.ng")
        self.assertEqual(call.kwargs["recipients"], [])

    def test_a_held_child_is_not_told_to_anybody(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.chiamaka,
            reason="Fees.", placed_by=self.bursar,
        )
        self.publish(self.principal)
        self.assertNotIn("Chiamaka", self.told(EVENT_RESULT_RELEASED_PARENT))
        self.assertNotIn("Chiamaka", self.told(EVENT_REPORT_CARD_READY))

    def test_another_schools_choice_does_not_reach_it(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.call(
                "post", self.solo_principal, "gradebook-publish", {"classes": [self.solo_class.pk]},
                term=self.solo_term,
            )
        self.assertEqual(self.told(EVENT_RESULT_RELEASED_PARENT), {})


class OneBranchSchoolTests(EveryParentFixture):
    def test_a_school_with_one_branch_tells_every_parent_when_it_chooses(self):
        set_setting(CFG_TELL_EVERY_PARENT, True, self.solo, self.solo_admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.call(
                "post", self.solo_principal, "gradebook-publish", {"classes": [self.solo_class.pk]},
                term=self.solo_term,
            )
        self.assertEqual(response.status_code, 200, response.data)
        [(recipients, metadata)] = self.told(EVENT_RESULT_RELEASED_PARENT).values()
        self.assertEqual(recipients, [self.mrs_okoro])
        self.assertEqual(metadata, {"student_id": self.solo_pupil.pk})
