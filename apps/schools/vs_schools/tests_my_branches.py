"""A school can read its own branches, and only its own.

Written because it could not. Every view in ``views/branch.py`` demands
``platform.branches.*``, which is PLATFORM-scoped and held by no school role, so
a live school administrator asking for her own branches was refused outright -
verified against lagoon-view before this existed.

The fix is deliberately the read half only. Creating and editing a branch stays
CodeX's, so opening those views (which share one key with create and update)
would have handed a school both.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_user.tokens import CodeXRefreshToken
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


class MyBranchesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        cls.main = make_branch(cls.school, name="Main Branch", is_main=True)
        cls.annex = make_branch(cls.school, name="Lekki Annex", is_main=False)
        cls.tenant = cls.school.tenant

        view = make_permission("school.branches.view", scope=PermissionScope.TENANT)
        role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(role, view)
        cls.admin = make_school_admin(
            None, email="amaka@bright-star.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)

        # A second school, to prove the scoping rather than assume it.
        cls.other = make_school(slug="greenfield", name="Greenfield")
        cls.other_branch = make_branch(cls.other, name="Greenfield Main", is_main=True)

    def client_for(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def test_a_school_reads_its_own_branches(self):
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        names = {row["name"] for row in response.data["data"]}
        self.assertEqual(names, {"Main Branch", "Lekki Annex"})

    def test_the_main_branch_leads(self):
        """Ordered the way a school talks about its own sites."""
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertTrue(response.data["data"][0]["is_main"])

    def test_another_school_is_not_in_the_list(self):
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        names = {row["name"] for row in response.data["data"]}
        self.assertNotIn("Greenfield Main", names)

    def test_a_code_this_school_does_not_have_is_a_404(self):
        """A branch this school has no code for is absent, not forbidden.

        Note that branch codes are PER SCHOOL, so Greenfield's code 1 and Bright
        Star's code 1 are different branches - asking for 1 correctly returns
        Bright Star's own. The case that matters is a code only the other school
        has, which is what this builds.
        """
        third = make_branch(self.other, name="Greenfield Annex", is_main=False)
        fourth = make_branch(self.other, name="Greenfield Third", is_main=False)
        # A code beyond anything Bright Star owns, held by Greenfield.
        self.assertGreater(fourth.code, self.annex.code)

        response = self.client_for(self.admin).get(
            reverse("my-branch-detail", kwargs={"code": fourth.code}),
            {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 404, response.data)
        del third

    def test_the_unbuilt_counts_are_null_rather_than_invented(self):
        """There is still no Student and no Teacher model in the product.

        Null says "not known" and the screen renders a dash. A zero would say
        the branch has no students, which is a different and false claim.

        ``classes_count`` was in this list until M13 landed a Class model, and
        the day it did this test had to choose: keep asserting null and freeze
        the count out, or move it. It moved, to
        BranchClassCountTests below, where a zero is asserted as a true claim
        rather than a missing one.
        """
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        row = response.data["data"][0]
        for field in ("students_count", "teachers_count"):
            self.assertIn(field, row, f"{field} must be in the shape already")
            self.assertIsNone(row[field])
        self.assertIn("classes_count", row)

    def test_a_reader_without_the_key_is_refused(self):
        stranger = make_school_admin(
            None, email="nobody@bright-star.example.com", tenant=self.tenant,
        )
        response = self.client_for(stranger).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_there_is_no_way_to_write(self):
        """Read-only on purpose: branches are CodeX's to create and edit.

        If somebody adds a POST or PATCH here, this fails and asks them to
        settle that question rather than let it arrive by accident.
        """
        client = self.client_for(self.admin)
        # The tenant assertion has to be on the URL or the request is refused
        # with 400 before it ever reaches a handler, which would make this pass
        # for the wrong reason.
        url = f"{reverse('my-branch-list')}?tenant={self.tenant.slug}"
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(
                url, {"name": "New Branch"}, format="json",
            )
            self.assertEqual(
                response.status_code, 405,
                f"{method.upper()} must not be allowed on {url}",
            )


class PendingSchoolBranchesTests(TestCase):
    """A school still onboarding can read its own branches.

    Written because it could not, and because the consequence was not a broken
    screen but an unreachable go-live: ``TaskKey.ACADEMIC_STRUCTURE`` is a
    required onboarding task, M13's screens scope every row to the whole school
    or to one branch, and that control reads this list. Shut, a two-branch
    school could never finish the task that would make it live.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(
            slug="brightfield", name="Brightfield Schools", status="PENDING",
        )
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)
        cls.tenant = cls.school.tenant

        view = make_permission("school.branches.view", scope=PermissionScope.TENANT)
        role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(role, view)
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)

    def setUp(self):
        # School.save() mirrors status onto the tenant; assert the fixture is
        # the shape this class is about rather than trusting it.
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, "PENDING")

    def client_for(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def test_a_pending_school_reads_its_own_branches(self):
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        names = {row["name"] for row in response.data["data"]}
        self.assertEqual(names, {"Lekki Branch", "Ikeja Branch"})

    def test_removing_the_declaration_refuses_with_tenant_not_live(self):
        """What proves the attribute is doing the work.

        Without this case the suite passes whether or not the declaration is
        there, because every other tenant in this file is ACTIVE.
        """
        from schools.vs_schools.views.my_branches import MyBranchListView

        with patch.object(MyBranchListView, "pending_tenant_surface", False):
            response = self.client_for(self.admin).get(
                reverse("my-branch-list"), {"tenant": self.tenant.slug},
            )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_the_detail_route_stays_shut_before_go_live(self):
        """Only the list was opened, and absence still means closed.

        Nothing before go-live reads one branch on its own. If that changes,
        this fails and asks for the decision rather than letting a second
        surface open by accident.
        """
        response = self.client_for(self.admin).get(
            reverse("my-branch-detail", kwargs={"code": self.lekki.code}),
            {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_an_active_school_is_unaffected(self):
        """The case an ACTIVE-only suite would have covered anyway.

        Here to prove opening the surface changed nothing for a live school,
        which is the half of the blast radius the PENDING cases cannot see.
        """
        self.school.status = "ACTIVE"
        self.school.save()
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]), 2)


class BranchClassCountTests(TestCase):
    """The count SchoolBranchSerializer promised would arrive with M13.

    It shipped as a hard ``None`` with a docstring saying it would become an
    annotation the day a Class model existed. M13 landed one, so these pin the
    three things that were decided when it did: live classes only, zero rather
    than null for a branch with none, and the shared classes of a school that
    has never used the branch column belonging to no branch's card.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch", is_main=False)

        view = make_permission("school.branches.view", scope=PermissionScope.TENANT)
        role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(role, view)
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)

    def setUp(self):
        import datetime

        from schools.vs_academics.models import (
            AcademicSession, Level, Program, SessionStatus,
        )

        # The academic structure belongs to a year now, so a level cannot exist
        # without one. A class carries its own copy of the same session rather
        # than joining through its level, and the two are held in step on every
        # write - so both are given the session built here.
        self.session = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027",
            start_date=datetime.date(2026, 9, 1),
            end_date=datetime.date(2027, 7, 31),
            status=SessionStatus.ACTIVE,
        )
        self.program = Program.all_objects.create(
            tenant=self.tenant, name="Junior Secondary", code="JSS",
        )
        self.level = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.session,
            name="JSS1", code="JSS1", order_index=1,
        )

    def klass(self, name, code, branch=None, active=True):
        from schools.vs_academics.models import SchoolClass

        return SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.level, session=self.session,
            name=name, code=code, branch=branch, is_active=active,
        )

    def rows(self):
        token = str(CodeXRefreshToken.for_user(self.admin).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {row["name"]: row for row in response.data["data"]}

    def test_a_branch_reports_its_own_live_classes(self):
        self.klass("JSS1 A", "JSS1-A", branch=self.lekki)
        self.klass("JSS1 B", "JSS1-B", branch=self.lekki)
        self.klass("JSS1 C", "JSS1-C", branch=self.ikeja)
        rows = self.rows()
        self.assertEqual(rows["Lekki Branch"]["classes_count"], 2)
        self.assertEqual(rows["Ikeja Branch"]["classes_count"], 1)

    def test_an_archived_class_is_not_counted(self):
        """Or the branch card disagrees with the class list beside it."""
        self.klass("JSS1 A", "JSS1-A", branch=self.lekki)
        self.klass("JSS1 B", "JSS1-B", branch=self.lekki, active=False)
        self.assertEqual(self.rows()["Lekki Branch"]["classes_count"], 1)

    def test_a_branch_with_no_classes_reports_zero_not_null(self):
        """Zero and null are different claims, and zero is the true one here."""
        rows = self.rows()
        self.assertEqual(rows["Lekki Branch"]["classes_count"], 0)
        self.assertIsNotNone(rows["Lekki Branch"]["classes_count"])

    def test_a_school_wide_class_belongs_to_no_branchs_card(self):
        """A class the school holds as a whole is not one branch's to claim.

        This is the shape a single-branch school writes for every class it has,
        so counting it on the main branch would double it into a total the
        school never stated.
        """
        self.klass("JSS1 A", "JSS1-A", branch=None)
        rows = self.rows()
        self.assertEqual(rows["Lekki Branch"]["classes_count"], 0)
        self.assertEqual(rows["Ikeja Branch"]["classes_count"], 0)

    def test_the_other_two_counts_are_still_null(self):
        """M11 and M12 have not landed, so a number for either would be invented."""
        row = self.rows()["Lekki Branch"]
        self.assertIsNone(row["students_count"])
        self.assertIsNone(row["teachers_count"])
