"""Staff in the product-wide search bar (``GET /v1/search/``).

The provider in ``search_places.py`` reuses the palette endpoint's matcher and
scoping. Brightfield has two branches, so a reader bound to Lekki is the case
that proves the scoping travelled with it.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase

from core.test_utils import TenantAPIClient
from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.models import StaffProfile
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    set_field_access,
)

URL = "/v1/search/"


class StaffSearchProviderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        view = make_permission("school.teachers.view", scope=PermissionScope.TENANT)
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja", is_main=False)
        role = make_role(cls.school, name="Head", key="head")
        make_role_permission(role, view)
        cls.admin = make_school_admin(None, email="adaeze@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.admin, role, branch=None)
        cls.lekki_head = make_school_admin(None, email="head@lekki.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.lekki_head, role, branch=cls.lekki)
        cls.nobody = make_school_admin(None, email="nobody@brightfield.test", tenant=cls.tenant)

        cls.bisi = cls._staff("bisi@lekki.test", "Bisi", "Lawal", cls.lekki)
        cls.bisi.staff_number = "BF-S-001"
        cls.bisi.save(update_fields=["staff_number"])

        # Open the register but read no first name and no job title; the
        # auditor reads no staff number either.
        install_declared_fields("school.teachers")
        cls.clerk = cls._restricted(
            view, "clerk", "school.teachers.first_name", "school.teachers.job_title",
        )
        cls.auditor = cls._restricted(
            view, "auditor", "school.teachers.job_title", "school.teachers.staff_number",
        )
        # Reads no first name and no email, which would carry the first name.
        cls.porter = cls._restricted(
            view, "porter", "school.teachers.first_name", "school.teachers.email",
        )
        cls._staff("bisola@ikeja.test", "Bisola", "Bello", cls.ikeja)
        cls._staff("bisade@brightfield.test", "Bisade", "Ojo", None)

        other = make_school(slug="greenfield", name="Greenfield Academy")
        make_branch(other, name="Main", is_main=True)
        user = make_school_admin(None, email="bisoye@greenfield.test", tenant=other.tenant,
                                 first_name="Bisoye", last_name="Adeyemi")
        StaffProfile.all_objects.create(
            tenant=other.tenant, user=user, job_title="Teacher",
            employment_status=EmploymentStatus.ACTIVE, hire_date=dt.date(2021, 9, 6),
        )

    @classmethod
    def _restricted(cls, view, key, *hidden):
        role = make_role(cls.school, name=key.title(), key=key)
        make_role_permission(role, view)
        set_field_access(role, *hidden, read=False, write=False)
        user = make_school_admin(None, email=f"{key}@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, user, role, branch=None)
        return user

    @classmethod
    def _staff(cls, email, first, last, branch):
        user = make_school_admin(
            None, email=email, tenant=cls.tenant, first_name=first, last_name=last,
        )
        return StaffProfile.all_objects.create(
            tenant=cls.tenant, user=user, branch=branch, job_title="Teacher",
            employment_status=EmploymentStatus.ACTIVE, hire_date=dt.date(2021, 9, 6),
        )

    def staff(self, user, q):
        response = TenantAPIClient(user).get(URL, {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return [h for h in response.data["data"]["people"] if h["type"] == "staff"]

    def test_a_member_finds_their_own_schools_staff_only(self):
        self.assertCountEqual(
            [h["label"] for h in self.staff(self.admin, "bis")],
            ["Bisi Lawal", "Bisola Bello", "Bisade Ojo"],
        )

    def test_a_branch_bound_reader_finds_their_branch_and_the_school_wide(self):
        self.assertCountEqual(
            [h["label"] for h in self.staff(self.lekki_head, "bis")],
            ["Bisi Lawal", "Bisade Ojo"],
        )

    def test_a_reader_without_the_staff_key_finds_nobody(self):
        self.assertEqual(self.staff(self.nobody, "bis"), [])

    def test_a_hit_carries_no_contact_details(self):
        for hit in self.staff(self.admin, "bis"):
            self.assertFalse({"email", "phone", "photo_url"} & set(hit), hit)
            self.assertEqual(hit["screen"], "staff.record")
            self.assertEqual(hit["meta"], "Teacher")

    # -- Field Access ---------------------------------------------------

    def palette(self, user, q):
        response = TenantAPIClient(user).get("/v1/i/me/staff/search/", {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_a_hidden_name_part_and_title_are_absent_from_the_palette_hit(self):
        hit = self.palette(self.clerk, "lawal")[0]
        self.assertNotIn("job_title", hit)
        self.assertEqual((hit["name"], hit["meta"]), ("Lawal", "BF-S-001"))
        self.assertNotIn("Bisi", repr(hit))
        self.assertNotIn("Teacher", repr(hit))

    def test_a_hidden_name_part_and_title_are_absent_from_the_search_bar_hit(self):
        hit = self.staff(self.clerk, "lawal")[0]
        self.assertNotIn("job_title", hit)
        self.assertEqual((hit["label"], hit["meta"]), ("Lawal", "BF-S-001"))
        self.assertNotIn("Bisi", repr(hit))
        self.assertNotIn("Teacher", repr(hit))

    def test_meta_is_empty_when_neither_title_nor_number_is_readable(self):
        hit = self.staff(self.auditor, "lawal")[0]
        self.assertFalse({"job_title", "staff_number"} & set(hit), hit)
        self.assertEqual((hit["label"], hit["meta"]), ("Bisi Lawal", ""))

    def test_a_default_reader_reads_every_field(self):
        hit = self.palette(self.admin, "lawal")[0]
        self.assertEqual(
            (hit["name"], hit["job_title"], hit["staff_number"], hit["meta"]),
            ("Bisi Lawal", "Teacher", "BF-S-001", "Teacher"),
        )

    # -- searching on a hidden name part --------------------------------

    def directory(self, user, q):
        response = TenantAPIClient(user).get("/v1/i/me/staff/", {"search": q})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        return rows.get("results", rows) if isinstance(rows, dict) else rows

    def test_a_hidden_first_name_finds_nobody_anywhere(self):
        self.assertEqual(self.staff(self.porter, "bisi"), [])
        self.assertEqual(self.palette(self.porter, "bisi"), [])
        self.assertEqual(self.directory(self.porter, "bisi"), [])

    def test_a_readable_surname_still_finds_them(self):
        self.assertEqual([h["label"] for h in self.staff(self.porter, "lawal")], ["Lawal"])
        self.assertEqual(len(self.palette(self.porter, "lawal")), 1)
        self.assertEqual(len(self.directory(self.porter, "lawal")), 1)

    def test_a_default_reader_still_finds_by_first_name(self):
        self.assertIn("Bisi Lawal", [h["label"] for h in self.staff(self.admin, "bisi")])
        self.assertTrue(self.directory(self.admin, "bisi"))


class StaffConditionalPlaceTests(TestCase):
    """"My staff record" and the postings screen exist only for some callers.

    Brightfield has two branches and Sunrise one. A member of staff with no
    key finds their own record; an administrator with no staff record does
    not. The postings screen is offered where the caller sees branches.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_config.models import Capability

        Capability.objects.create(key="teachers", label="Teachers")
        update = make_permission("school.teachers.update", scope=PermissionScope.TENANT)
        cls.admins = {}
        cls.members = {}
        for slug, branches in (("brightfield", 2), ("sunrise", 1)):
            school = make_school(slug=slug, name=slug.title())
            main = make_branch(school, name="Main", is_main=True)
            if branches == 2:
                make_branch(school, name="Annex", is_main=False)
            role = make_role(school, name="Admin", key="admin")
            make_role_permission(role, update)
            admin = make_school_admin(None, email=f"admin@{slug}.test", tenant=school.tenant)
            make_assignment(school, admin, role, branch=None)
            cls.admins[slug] = admin
            member = make_school_admin(None, email=f"teacher@{slug}.test", tenant=school.tenant)
            StaffProfile.all_objects.create(
                tenant=school.tenant, user=member, branch=main, job_title="Teacher",
                employment_status=EmploymentStatus.ACTIVE, hire_date=dt.date(2021, 9, 6),
            )
            cls.members[slug] = member

    def actions(self, user, q):
        response = TenantAPIClient(user).get(URL, {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return [hit["key"] for hit in response.data["data"]["actions"]]

    def test_my_staff_record_is_offered_only_to_somebody_who_has_one(self):
        for slug in ("brightfield", "sunrise"):
            with self.subTest(school=slug):
                self.assertIn("staff.my_record", self.actions(self.members[slug], "my staff"))
                self.assertNotIn("staff.my_record", self.actions(self.admins[slug], "my staff"))

    def test_postings_are_offered_only_where_the_caller_sees_branches(self):
        self.assertIn("staff.posting", self.actions(self.admins["brightfield"], "postings"))
        self.assertNotIn("staff.posting", self.actions(self.admins["sunrise"], "postings"))
