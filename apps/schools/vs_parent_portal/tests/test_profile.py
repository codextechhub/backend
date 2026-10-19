"""A parent's own profile: what the school manages and the contact details they edit.

Mrs. Nwosu changes her own phone numbers and address, and the change is written
to her guardian row and audited with her as the actor. Her name, the address
she signs in with and her children are shown and cannot be changed here, and a
request that names any other field is refused rather than ignored. Her husband
shares the family record; nothing of his appears and nothing of his is touched.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian
from vs_audit.models import AuditEvent

from .base import HUSBAND_ADDRESS, HUSBAND_EMAIL, HUSBAND_PHONE, ParentFixture

PROFILE = "parent-portal-profile"
ME = "parent-portal-me"


class ProfileTests(ParentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = cls.lekki_head
        cls.teacher_guardian = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Head", last_name="Teacher",
            phone="08110000002", email=cls.teacher.email, user=cls.teacher,
        )
        ife = cls.child("Ife", "Teacher", "BFS/201", cls.lekki, cls.lekki_class, dob=(2014, 3, 3))
        cls.link(cls, ife, cls.teacher_guardian, relationship=Relationship.FATHER)

    def get(self, user, **kwargs):
        return TenantAPIClient(user, **kwargs).get(reverse(PROFILE))

    def patch(self, user, data):
        return TenantAPIClient(user).patch(reverse(PROFILE), data, format="json")

    def audit(self, guardian):
        return AuditEvent.objects.filter(entity_type="Guardian", entity_id=str(guardian.pk))

    # Access

    def test_a_member_of_staff_with_no_guardian_record_gets_404(self):
        for call in (lambda: self.get(self.admin), lambda: self.patch(self.admin, {"phone": "0803 456 7890"})):
            response = call()
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["error"]["code"], "NOT_A_PARENT")

    def test_a_parent_of_one_school_cannot_assert_another(self):
        response = self.get(self.nwosu_user, tenant_slug=self.solo.slug)
        self.assertEqual(response.status_code, 404)

    # Read

    def test_the_profile_splits_what_the_school_manages_from_what_the_parent_edits(self):
        data = self.get(self.nwosu_user).data["data"]
        managed = data["managed_by_school"]
        self.assertEqual(managed["full_name"], "Adaeze Nwosu")
        self.assertEqual(managed["sign_in_email"], "adaeze.nwosu@example.ng")
        self.assertEqual(
            data["editable"], {"phone": "08110000000", "alt_phone": "", "address": ""},
        )
        self.assertIs(data["email_editable"], False)

    def test_the_profile_carries_nothing_of_the_other_guardian(self):
        body = self.get(self.nwosu_user).content.decode()
        for private in (HUSBAND_PHONE, HUSBAND_EMAIL, HUSBAND_ADDRESS, "Chukwudi"):
            self.assertNotIn(private, body)

    def test_children_show_their_branch_where_the_school_has_several(self):
        children = self.get(self.nwosu_user).data["data"]["managed_by_school"]["children"]
        self.assertEqual([c["first_name"] for c in children], ["Chiamaka", "Tobi", "Folu"])
        self.assertEqual(children[0]["admission_number"], "BFS/001")
        self.assertEqual(children[0]["class_name"], "JSS1 A")
        self.assertEqual(
            {c["first_name"]: c["branch"]["name"] for c in children},
            {"Chiamaka": "Lekki", "Tobi": "Ikeja", "Folu": "Epe"},
        )

    def test_a_one_branch_school_shows_no_branch(self):
        children = self.get(self.ade_user).data["data"]["managed_by_school"]["children"]
        self.assertEqual([c["first_name"] for c in children], ["Bola"])
        self.assertNotIn("branch", children[0])

    # Write

    def test_the_parent_changes_their_contact_details_and_it_is_audited_once(self):
        response = self.patch(self.nwosu_user, {
            "phone": "0803 456 7890", "alt_phone": "+234 802 111 2222",
            "address": "  7 Palm Grove, Ikeja  ",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["editable"], {
            "phone": "0803 456 7890", "alt_phone": "+234 802 111 2222",
            "address": "7 Palm Grove, Ikeja",
        })
        self.nwosu.refresh_from_db()
        self.assertEqual(self.nwosu.phone, "0803 456 7890")
        self.assertEqual(self.nwosu.alt_phone, "+234 802 111 2222")
        self.assertEqual(self.nwosu.address, "7 Palm Grove, Ikeja")
        events = self.audit(self.nwosu)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().actor_user_id, self.nwosu_user.pk)

    def test_an_unchanged_save_writes_no_audit_entry(self):
        response = self.patch(self.nwosu_user, {"phone": "08110000000", "alt_phone": "", "address": ""})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.audit(self.nwosu).count(), 0)

    def test_the_other_guardian_on_the_family_record_is_untouched(self):
        self.patch(self.nwosu_user, {"phone": "0803 456 7890", "address": "7 Palm Grove"})
        self.husband.refresh_from_db()
        self.assertEqual(
            (self.husband.phone, self.husband.email, self.husband.address),
            (HUSBAND_PHONE, HUSBAND_EMAIL, HUSBAND_ADDRESS),
        )

    def test_a_bad_phone_is_refused_on_its_field_and_nothing_changes(self):
        response = self.patch(self.nwosu_user, {"phone": "12", "address": "7 Palm Grove"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["detail"]["field"], "phone")
        self.nwosu.refresh_from_db()
        self.assertEqual((self.nwosu.phone, self.nwosu.address), ("08110000000", ""))
        self.assertEqual(self.audit(self.nwosu).count(), 0)

    def test_a_bad_second_phone_is_refused_on_its_own_field(self):
        response = self.patch(self.nwosu_user, {"alt_phone": "call me"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["detail"]["field"], "alt_phone")

    def test_the_main_phone_cannot_be_blanked(self):
        for blank in ("", "   ", None):
            response = self.patch(self.nwosu_user, {"phone": blank})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["error"]["detail"]["field"], "phone")
        self.nwosu.refresh_from_db()
        self.assertEqual(self.nwosu.phone, "08110000000")

    def test_the_second_phone_can_be_cleared(self):
        self.patch(self.nwosu_user, {"alt_phone": "0803 456 7890"})
        response = self.patch(self.nwosu_user, {"alt_phone": ""})
        self.assertEqual(response.status_code, 200, response.data)
        self.nwosu.refresh_from_db()
        self.assertEqual(self.nwosu.alt_phone, "")

    def test_an_address_over_500_characters_is_refused(self):
        response = self.patch(self.nwosu_user, {"address": "x" * 501})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["detail"]["field"], "address")
        self.assertEqual(self.patch(self.nwosu_user, {"address": "x" * 500}).status_code, 200)

    def test_a_field_the_parent_may_not_change_is_refused_by_name(self):
        for field, value in (
            ("email", "new@example.ng"), ("first_name", "Someone"), ("occupation", "Pilot"),
        ):
            with self.subTest(field=field):
                response = self.patch(self.nwosu_user, {"phone": "0803 456 7890", field: value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["error"]["detail"]["field"], field)
                self.assertEqual(response.data["message"], "This cannot be changed here. Ask the school.")
        self.nwosu.refresh_from_db()
        self.assertEqual(
            (self.nwosu.phone, self.nwosu.email, self.nwosu.first_name),
            ("08110000000", "adaeze.nwosu@example.ng", "Adaeze"),
        )
        self.assertEqual(self.audit(self.nwosu).count(), 0)

    def test_a_body_that_is_not_an_object_is_refused(self):
        response = self.patch(self.nwosu_user, ["phone"])
        self.assertEqual(response.status_code, 400)

    def test_the_parent_keeps_portal_access_after_an_edit(self):
        self.patch(self.nwosu_user, {"phone": "0803 456 7890"})
        client = TenantAPIClient(self.nwosu_user)
        self.assertEqual(client.get(reverse(ME)).status_code, 200)
        self.assertEqual(self.get(self.nwosu_user).status_code, 200)

    def test_a_teacher_who_is_also_a_parent_edits_only_her_guardian_contact(self):
        before = (self.teacher.email, self.teacher.first_name, self.teacher.last_name)
        response = self.patch(self.teacher, {"phone": "0803 456 7890", "address": "2 Lekki Close"})
        self.assertEqual(response.status_code, 200, response.data)
        self.teacher_guardian.refresh_from_db()
        self.assertEqual(self.teacher_guardian.phone, "0803 456 7890")
        self.assertEqual(self.teacher_guardian.user_id, self.teacher.pk)
        self.teacher.refresh_from_db()
        self.assertEqual((self.teacher.email, self.teacher.first_name, self.teacher.last_name), before)
        self.assertEqual(TenantAPIClient(self.teacher).get(reverse(ME)).status_code, 200)
