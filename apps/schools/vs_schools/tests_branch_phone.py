"""A branch has its own phone, and a new school's main branch starts with the school's.

Bright Star is created with one branch and gives only the school's number. That
number becomes the main branch's phone, so its payment terminal can notify
someone from the first day. A branch added later, or one created with a number of
its own, keeps what it was given. The number is a switched field and is never
written into the audit trail.
"""
import json

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_audit.models import AuditEvent
from vs_rbac.tests.helpers import (
    assert_school_created,
    make_branch,
    make_school,
    make_vision_user,
    with_school_billing_details,
)
from vs_tenants.models import Branch

from .models import School, SchoolStatus

SCHOOL_PHONE = "08099990000"


def _branch(name, *, is_main, email, **extra):
    return {
        "name": name, "state": "Lagos", "is_main": is_main,
        "primary_admin_data": {"full_name": f"{name} Head", "email": email}, **extra,
    }


class BranchPhoneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(email="branch-phone@example.com", super_admin=True)

    def api(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def create_school(self, slug, branches):
        response = self.api().post(
            reverse("school-create"),
            with_school_billing_details({
                "name": slug.title(), "slug": slug, "phone": SCHOOL_PHONE, "branches": branches}),
            format="json",
        )
        assert_school_created(self, response)
        return School.objects.get(slug=slug)

    # -- school creation ---------------------------------------------------------- #

    def test_the_main_branch_of_a_new_school_takes_the_schools_phone(self):
        school = self.create_school("bright-star", [
            _branch("Main Branch", is_main=True, email="head@bright-star.test")])
        self.assertEqual(school.main_branch.phone, SCHOOL_PHONE)

    def test_a_lone_branch_promoted_to_main_takes_it_too(self):
        school = self.create_school("lone-branch", [
            _branch("Only Branch", is_main=False, email="head@lone-branch.test")])
        self.assertTrue(school.main_branch.is_main)
        self.assertEqual(school.main_branch.phone, SCHOOL_PHONE)

    def test_a_number_given_to_the_main_branch_is_kept_and_the_others_get_none(self):
        school = self.create_school("two-sites", [
            _branch("Main Branch", is_main=True, email="a@two-sites.test", phone="08011112222"),
            _branch("Annex", is_main=False, email="b@two-sites.test"),
        ])
        self.assertEqual(school.main_branch.phone, "08011112222")
        self.assertEqual(Branch.all_objects.get(tenant=school.tenant, name="Annex").phone, "")

    def test_the_annex_keeps_the_number_it_was_given_and_the_main_branch_the_schools(self):
        school = self.create_school("annex-phone", [
            _branch("Main Branch", is_main=True, email="a@annex-phone.test"),
            _branch("Annex", is_main=False, email="b@annex-phone.test", phone="08077778888"),
        ])
        self.assertEqual(school.main_branch.phone, SCHOOL_PHONE)
        self.assertEqual(
            Branch.all_objects.get(tenant=school.tenant, name="Annex").phone, "08077778888")

    def test_a_branch_phone_that_is_not_a_number_is_refused(self):
        response = self.api().post(
            reverse("school-create"),
            with_school_billing_details({
                "name": "Bad Phone", "slug": "bad-phone", "phone": SCHOOL_PHONE,
                "branches": [_branch("Main", is_main=True, email="h@bad-phone.test",
                                     phone="call me")]}),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(School.objects.filter(slug="bad-phone").exists())

    # -- the branch endpoints ------------------------------------------------------ #

    def active_school(self, slug):
        school = make_school(slug=slug, name=slug.title(), status=SchoolStatus.ACTIVE)
        branch = make_branch(school, name="Main Branch", phone="08011112222")
        return school, branch

    def update_url(self, school, branch):
        return reverse("branch-update", kwargs={"slug": school.slug, "code": branch.code})

    def test_the_phone_is_read_on_the_detail_and_changed_by_an_update(self):
        school, branch = self.active_school("phone-reads")
        detail = self.api().get(reverse(
            "branch-detail", kwargs={"slug": school.slug, "code": branch.code}))
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["data"]["phone"], "08011112222")

        response = self.api().patch(
            self.update_url(school, branch), {"phone": "08033334444"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        branch.refresh_from_db()
        self.assertEqual(branch.phone, "08033334444")

    def test_an_update_with_a_bad_number_is_refused(self):
        school, branch = self.active_school("phone-bad")
        response = self.api().patch(
            self.update_url(school, branch), {"phone": "abc"}, format="json")
        self.assertEqual(response.status_code, 400)
        branch.refresh_from_db()
        self.assertEqual(branch.phone, "08011112222")

    def test_a_branch_added_later_takes_its_own_phone(self):
        school, _ = self.active_school("phone-added")
        response = self.api().post(
            reverse("branch-create", kwargs={"slug": school.slug}),
            {"name": "Lekki Branch", "state": "Lagos", "phone": "08044445555",
             "primary_admin_data": {"full_name": "Lekki Head", "email": "lekki@phone-added.test"}},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Branch.all_objects.get(tenant=school.tenant, name="Lekki Branch").phone, "08044445555")

    def test_the_number_is_never_written_into_the_audit_trail(self):
        school, branch = self.active_school("phone-audit")
        self.api().patch(self.update_url(school, branch),
                         {"phone": "08033334444", "address": "5 Allen Avenue"}, format="json")
        self.api().post(
            reverse("branch-create", kwargs={"slug": school.slug}),
            {"name": "Lekki Branch", "state": "Lagos", "phone": "08044445555",
             "primary_admin_data": {"full_name": "Lekki Head", "email": "lekki@phone-audit.test"}},
            format="json",
        )
        events = AuditEvent.objects.filter(entity_type="Branch")
        self.assertGreaterEqual(events.count(), 2)
        text = json.dumps(list(events.values_list("summary", "before_data", "diff_data", "metadata")))
        for number in ("08033334444", "08044445555", "08011112222"):
            self.assertNotIn(number, text)
        self.assertIn("5 Allen Avenue", text)  # Other columns are audited as before.
