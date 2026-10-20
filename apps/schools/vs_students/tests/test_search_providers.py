"""Students and guardians in the product-wide search bar (``GET /v1/search/``).

The providers in ``search_places.py`` reuse the palette endpoints' own
matchers, so these tests ask the search bar the questions those endpoints
already answer: whose people a reader finds, which branch's, and through which
field switches.

Brightfield has two branches, Lekki and Ikeja, so branch scoping is live;
Greenfield is another tenant with one branch.
"""
from __future__ import annotations

import datetime as dt

from django.test import SimpleTestCase, TestCase

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import Guardian, Student, StudentGuardian
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
from vs_search import registry

URL = "/v1/search/"


class ProviderOrderTests(SimpleTestCase):
    def test_students_then_guardians_then_staff(self):
        names = [fn.__name__ for fn in registry.people_providers()]
        self.assertEqual(names, ["find_students", "find_guardians", "find_staff"])


class StudentSearchProviderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        view = make_permission("school.students.view", scope=PermissionScope.TENANT)

        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja", is_main=False)
        cls.registrar_role = make_role(cls.school, name="Registrar", key="registrar")
        make_role_permission(cls.registrar_role, view)
        cls.admin = make_school_admin(None, email="adaeze@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.admin, cls.registrar_role, branch=None)
        cls.lekki_head = make_school_admin(None, email="head@lekki.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.lekki_head, cls.registrar_role, branch=cls.lekki)
        # Signed in, holding nothing.
        cls.nobody = make_school_admin(None, email="nobody@brightfield.test", tenant=cls.tenant)
        # Opens students and guardians, but reads no first name of either, and
        # no guardian email (which would carry the guardian's first name).
        install_declared_fields("school.students")
        install_declared_fields("school.guardians")
        clerk_role = make_role(cls.school, name="Clerk", key="clerk")
        make_role_permission(clerk_role, view)
        set_field_access(
            clerk_role, "school.guardians.first_name", "school.students.first_name",
            "school.guardians.email",
            read=False, write=False,
        )
        cls.clerk = make_school_admin(None, email="clerk@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.clerk, clerk_role, branch=None)

        cls.lekki_pupil = cls._student(cls.tenant, cls.lekki, "Adaora", "Okafor", "BF-001")
        cls._student(cls.tenant, cls.ikeja, "Adamma", "Eze", "BF-002")
        father = Guardian.all_objects.create(
            tenant=cls.tenant, full_name="Adamu Okafor", phone="08035550101",
            email="adamu@example.ng",
        )
        StudentGuardian.all_objects.create(
            tenant=cls.tenant, student=cls.lekki_pupil, guardian=father,
            relationship=Relationship.FATHER, is_primary=True,
        )

        cls.other = make_school(slug="greenfield", name="Greenfield Academy")
        other_branch = make_branch(cls.other, name="Main", is_main=True)
        other_role = make_role(cls.other, name="Registrar", key="registrar")
        make_role_permission(other_role, view)
        cls.other_admin = make_school_admin(
            None, email="head@greenfield.test", tenant=cls.other.tenant,
        )
        make_assignment(cls.other, cls.other_admin, other_role, branch=None)
        cls._student(cls.other.tenant, other_branch, "Adanna", "Obi", "GF-001")

    @staticmethod
    def _student(tenant, branch, first, last, number):
        return Student.all_objects.create(
            tenant=tenant, branch=branch, first_name=first, last_name=last,
            student_number=number, date_of_birth=dt.date(2013, 4, 18),
            gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
            enrolment_date=dt.date(2025, 9, 1),
        )

    def people(self, user, q, kind=None):
        response = TenantAPIClient(user).get(URL, {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return [h for h in response.data["data"]["people"] if kind in (None, h["type"])]

    @staticmethod
    def labels(hits):
        return [hit["label"] for hit in hits]

    def test_a_member_never_finds_another_schools_students(self):
        self.assertCountEqual(
            self.labels(self.people(self.admin, "ada", "student")),
            ["Adaora Okafor", "Adamma Eze"],
        )
        self.assertEqual(
            self.labels(self.people(self.other_admin, "ada", "student")), ["Adanna Obi"],
        )

    def test_a_branch_bound_reader_finds_only_their_branchs_students(self):
        self.assertEqual(
            self.labels(self.people(self.lekki_head, "ada", "student")), ["Adaora Okafor"],
        )

    def test_a_reader_without_the_students_key_finds_nobody(self):
        self.assertEqual(self.people(self.nobody, "ada"), [])

    def test_students_come_before_guardians(self):
        hits = self.people(self.admin, "okafor")
        self.assertEqual(
            [(h["type"], h["label"], h["screen"]) for h in hits],
            [("student", "Adaora Okafor", "students.record"),
             ("guardian", "Adamu Okafor", "students.guardian_record")],
        )
        self.assertEqual(hits[0]["meta"], "BF-001")
        self.assertEqual(hits[1]["meta"], "Adaora Okafor")

    def test_a_hit_carries_no_contact_details(self):
        for hit in self.people(self.admin, "okafor"):
            self.assertFalse({"email", "phone", "photo_url"} & set(hit), hit)
            self.assertTrue({"type", "id", "label", "meta", "screen"} <= set(hit), hit)

    def test_a_student_hit_follows_the_readers_field_access(self):
        """A hidden admission number is absent from the hit, its meta and the palette.

        Both read through one render that carries the request, which is what
        lets the switches the hit serializer declares apply to its reader.
        """
        install_declared_fields("school.students")
        set_field_access(
            self.registrar_role, "school.students.student_number", read=False, write=False,
        )

        hit = self.people(self.admin, "adaora", "student")[0]
        self.assertNotIn("student_number", hit)
        self.assertNotIn("BF-001", hit["meta"])

        response = TenantAPIClient(self.admin).get("/v1/students/search/", {"q": "adaora"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("student_number", response.data["data"][0])


    # -- guardian hits and Field Access ---------------------------------
    # The clerk may read neither a guardian's nor a pupil's first name. Their
    # hit for Adamu Okafor, father of Adaora, reads "Okafor" beside "Okafor".

    def palette(self, user, q):
        response = TenantAPIClient(user).get("/v1/guardians/search/", {"q": q})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_a_hidden_guardian_name_part_is_absent_from_the_palette_hit(self):
        hit = self.palette(self.clerk, "okafor")[0]
        self.assertNotIn("first_name", hit)
        self.assertEqual(hit["full_name"], "Okafor")
        self.assertEqual(hit["ward_names"], ["Okafor"])
        self.assertNotIn("Ada", repr(hit))

    def test_a_hidden_name_part_is_absent_from_the_search_bar_hit(self):
        hit = self.people(self.clerk, "okafor", "guardian")[0]
        self.assertNotIn("first_name", hit)
        self.assertEqual((hit["label"], hit["meta"]), ("Okafor", "Okafor"))
        self.assertNotIn("Ada", repr(hit))

    def test_a_default_reader_reads_every_name_part(self):
        hit = self.palette(self.admin, "adamu")[0]
        self.assertEqual(
            (hit["first_name"], hit["full_name"], hit["ward_names"]),
            ("Adamu", "Adamu Okafor", ["Adaora Okafor"]),
        )
        bar = self.people(self.admin, "adamu", "guardian")[0]
        self.assertEqual((bar["label"], bar["meta"]), ("Adamu Okafor", "Adaora Okafor"))

    def test_the_directory_names_wards_by_the_same_switches(self):
        response = TenantAPIClient(self.clerk).get("/v1/guardians/", {"search": "okafor"})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertEqual(rows[0]["ward_names"], ["Okafor"])

    # -- searching on a hidden name part --------------------------------
    # The clerk cannot read first names. Typing "Adaora" or "Adamu" must find
    # nobody anywhere, or a blank-labelled hit would say the name is there;
    # typing the surname they can read still finds both.

    def directory(self, user, path, q):
        response = TenantAPIClient(user).get(path, {"search": q})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        return rows.get("results", rows) if isinstance(rows, dict) else rows

    def test_a_hidden_first_name_finds_no_pupil_anywhere(self):
        self.assertEqual(self.people(self.clerk, "adaora", "student"), [])
        response = TenantAPIClient(self.clerk).get("/v1/students/search/", {"q": "adaora"})
        self.assertEqual(response.data["data"], [])
        self.assertEqual(self.directory(self.clerk, "/v1/students/", "adaora"), [])

    def test_a_hidden_first_name_finds_no_guardian_anywhere(self):
        self.assertEqual(self.people(self.clerk, "adamu", "guardian"), [])
        self.assertEqual(self.palette(self.clerk, "adamu"), [])
        self.assertEqual(self.directory(self.clerk, "/v1/guardians/", "adamu"), [])

    def test_a_readable_surname_still_finds_both(self):
        self.assertEqual(
            [(h["type"], h["label"]) for h in self.people(self.clerk, "okafor")],
            [("student", "Okafor"), ("guardian", "Okafor")],
        )
        self.assertEqual(len(self.directory(self.clerk, "/v1/students/", "okafor")), 1)

    def test_a_default_reader_still_finds_by_first_name(self):
        self.assertEqual(
            self.labels(self.people(self.admin, "adaora", "student")), ["Adaora Okafor"],
        )
        self.assertEqual(
            self.labels(self.people(self.admin, "adamu", "guardian")), ["Adamu Okafor"],
        )
        self.assertEqual(len(self.directory(self.admin, "/v1/students/", "adaora")), 1)
        self.assertEqual(len(self.directory(self.admin, "/v1/guardians/", "adamu")), 1)

    def test_student_searches_share_multiword_and_middle_name_matching(self):
        self.lekki_pupil.middle_name = "Chisom"
        self.lekki_pupil.save(update_fields=["middle_name"])
        for query in ("adaora okafor", "okafor ada", "chisom", "ada chi"):
            with self.subTest(query=query):
                palette = TenantAPIClient(self.admin).get("/v1/students/search/", {"q": query})
                self.assertEqual(palette.status_code, 200, palette.data)
                self.assertEqual([row["id"] for row in palette.data["data"]], [self.lekki_pupil.id])
                self.assertEqual(
                    self.labels(self.people(self.admin, query, "student")),
                    ["Adaora Chisom Okafor"],
                )
                self.assertEqual(
                    [row["id"] for row in self.directory(self.admin, "/v1/students/", query)],
                    [self.lekki_pupil.id],
                )

    def test_directory_search_finds_a_departed_student(self):
        self.lekki_pupil.status = StudentStatus.GRADUATED
        self.lekki_pupil.save(update_fields=["status"])
        self.assertEqual(
            [row["id"] for row in self.directory(self.admin, "/v1/students/", "adaora")],
            [self.lekki_pupil.id],
        )
