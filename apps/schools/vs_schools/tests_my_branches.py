"""A school's staff can read its own branches, and only its own.

``/v1/i/me/branches/`` is the read half of branch management: CodeX creates and
edits branches, so the platform's branch views (which share one key with create
and update) stay closed to a school.

Who may read it is anybody holding a role at the school, whatever that role is
called, because every form that files a record under a branch builds its
branch question from this list. Parents and pupils hold no role and are refused.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import Guardian, Student
from vs_rbac.models import PermissionScope, TenantUserRoleAssignment
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
from vs_user.tokens import CodeXRefreshToken


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

        Branch codes are per school, so Greenfield's code 1 and Bright Star's
        code 1 are different branches, and asking for 1 returns Bright Star's
        own. The case that matters is a code only the other school has.
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

        ``classes_count`` is not in this list. It has a Class model behind it,
        so it is asserted in BranchClassCountTests below, where a zero is a true
        claim rather than a missing one.
        """
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        row = response.data["data"][0]
        for field in ("students_count", "teachers_count"):
            self.assertIn(field, row, f"{field} must be in the shape already")
            self.assertIsNone(row[field])
        self.assertIn("classes_count", row)

    def test_an_account_holding_no_role_is_refused(self):
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


class WhoMayReadTests(TestCase):
    """Every member of a school's staff reads its branches; nobody else does.

    Lagoon View has two branches, Ikoyi and Ajah. Ngozi is its Finance Admin,
    Tunde teaches, and Kemi holds Admissions Officer, a role the school made
    itself with no keys in it. None of them holds ``school.branches.view``.
    Mrs. Okafor is a parent with a guardian record, and her son Emeka is a
    pupil with a portal account; neither holds a role. Greenfield is another
    school, with a Finance Admin of its own.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="lagoon-view", name="Lagoon View")
        cls.tenant = cls.school.tenant
        cls.ikoyi = make_branch(cls.school, name="Ikoyi Branch", is_main=True)
        cls.ajah = make_branch(cls.school, name="Ajah Branch", is_main=False)

        claim = make_permission(
            "finance.expense_claim.create", scope=PermissionScope.TENANT,
        )
        finance_admin = make_role(cls.school, name="Finance Admin", key="finance_admin")
        make_role_permission(finance_admin, claim)
        teacher = make_role(cls.school, name="Teacher", key="teacher")
        admissions = make_role(cls.school, name="Admissions Officer")

        cls.ngozi = cls.staff("ngozi@lagoon-view.example.com", finance_admin)
        cls.tunde = cls.staff("tunde@lagoon-view.example.com", teacher, branch=cls.ajah)
        cls.kemi = cls.staff("kemi@lagoon-view.example.com", admissions)

        cls.parent_account = make_school_admin(
            None, email="mrs.okafor@example.ng", tenant=cls.tenant,
        )
        Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Ada", last_name="Okafor",
            phone="08030000000", email=cls.parent_account.email,
            user=cls.parent_account,
        )
        cls.pupil_account = make_school_admin(
            None, email="emeka.okafor@example.ng", tenant=cls.tenant,
        )
        Student.all_objects.create(
            tenant=cls.tenant, branch=cls.ikoyi, first_name="Emeka",
            last_name="Okafor", student_number="LV/0001",
            date_of_birth=dt.date(2014, 3, 2), gender=Gender.MALE,
            status=StudentStatus.ACTIVE, enrolment_date=dt.date(2025, 9, 8),
            user=cls.pupil_account,
        )

        cls.greenfield = make_school(slug="greenfield", name="Greenfield")
        make_branch(cls.greenfield, name="Greenfield Main", is_main=True)
        greenfield_finance = make_role(
            cls.greenfield, name="Finance Admin", key="finance_admin",
        )
        make_role_permission(greenfield_finance, claim)
        cls.greenfield_bursar = make_school_admin(
            None, email="bursar@greenfield.example.com",
            tenant=cls.greenfield.tenant,
        )
        make_assignment(cls.greenfield, cls.greenfield_bursar, greenfield_finance, branch=None)

    @classmethod
    def staff(cls, email, role, branch=None):
        user = make_school_admin(branch, email=email, tenant=cls.tenant)
        make_assignment(cls.school, user, role, branch=branch)
        return user

    def listing(self, user, tenant=None):
        client = TenantAPIClient(user=user, tenant_slug=tenant.slug if tenant else None)
        return client.get(reverse("my-branch-list"))

    def names(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        return {row["name"] for row in response.data["data"]}

    def test_the_finance_admin_reads_both_branches(self):
        """The list her expense claim form decides its branch question from."""
        self.assertEqual(
            self.names(self.listing(self.ngozi)), {"Ikoyi Branch", "Ajah Branch"},
        )

    def test_a_teacher_reads_the_list(self):
        """A teacher posted to one branch still reads the whole list.

        The list says which branches the school runs; which ones a reader may
        file under is the session's branch reach, not this endpoint's.
        """
        self.assertEqual(
            self.names(self.listing(self.tunde)), {"Ikoyi Branch", "Ajah Branch"},
        )

    def test_a_role_the_school_made_itself_reads_the_list(self):
        self.assertEqual(
            self.names(self.listing(self.kemi)), {"Ikoyi Branch", "Ajah Branch"},
        )

    def test_a_teacher_reads_one_branch_in_full(self):
        client = TenantAPIClient(user=self.tunde)
        response = client.get(reverse("my-branch-detail", kwargs={"code": self.ikoyi.code}))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["name"], "Ikoyi Branch")

    def test_a_parent_is_refused(self):
        self.assertEqual(self.listing(self.parent_account).status_code, 403)

    def test_a_pupil_is_refused(self):
        self.assertEqual(self.listing(self.pupil_account).status_code, 403)

    def test_a_parent_and_a_pupil_are_refused_the_detail_too(self):
        for account in (self.parent_account, self.pupil_account):
            response = TenantAPIClient(user=account).get(
                reverse("my-branch-detail", kwargs={"code": self.ikoyi.code}),
            )
            self.assertEqual(response.status_code, 403, account.email)

    def test_a_revoked_role_no_longer_admits(self):
        TenantUserRoleAssignment.objects.filter(user=self.kemi).update(
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.REVOKED,
        )
        self.assertEqual(self.listing(self.kemi).status_code, 403)

    def test_another_schools_branches_never_appear(self):
        self.assertNotIn("Greenfield Main", self.names(self.listing(self.ngozi)))
        self.assertEqual(
            self.names(self.listing(self.greenfield_bursar)), {"Greenfield Main"},
        )

    def test_another_schools_staff_cannot_read_this_one(self):
        """Holding a role at Greenfield is not holding one at Lagoon View."""
        response = self.listing(self.greenfield_bursar, tenant=self.tenant)
        self.assertIn(response.status_code, (403, 404), response.data)
        self.assertNotIn("data", response.data)


class TenantReferenceListsTests(WhoMayReadTests):
    """The other lists every member of staff needs, behind the same rule.

    ``/v1/tenants/branches/`` is the branch picker a payroll or procurement
    form files under, and ``/v1/config/my-capabilities/`` is what the school's
    plan includes, which draws the staff navigation. Both are open to every
    member of Lagoon View's staff and to nobody else.
    """

    LISTS = ("/v1/tenants/branches/", "/v1/config/my-capabilities/")

    def get(self, user, url, tenant=None):
        return TenantAPIClient(
            user=user, tenant_slug=tenant.slug if tenant else None,
        ).get(url)

    def test_every_member_of_staff_reads_them(self):
        for user in (self.ngozi, self.tunde, self.kemi):
            for url in self.LISTS:
                with self.subTest(user=user.email, url=url):
                    response = self.get(user, url)
                    self.assertEqual(response.status_code, 200, response.data)

    def test_a_parent_and_a_pupil_are_refused(self):
        for user in (self.parent_account, self.pupil_account):
            for url in self.LISTS:
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.get(user, url).status_code, 403)

    def test_another_schools_staff_are_refused(self):
        for url in self.LISTS:
            with self.subTest(url=url):
                response = self.get(self.greenfield_bursar, url, tenant=self.tenant)
                self.assertIn(response.status_code, (403, 404), response.data)
                self.assertNotIn("data", response.data)

    def test_staff_with_only_a_personal_grant_read_them(self):
        """Staff standing comes from any grant, not only from a role."""
        from vs_rbac.models import UserPermissionOverride

        covering = make_school_admin(
            None, email="covering@lagoon-view.example.com", tenant=self.tenant,
        )
        UserPermissionOverride.objects.create(
            tenant=self.tenant, user=covering,
            permission=make_permission("finance.payroll.view"),
            mode=UserPermissionOverride.Mode.ALLOW, reason="Covering payroll.",
        )
        for url in (*self.LISTS, reverse("my-branch-list")):
            with self.subTest(url=url):
                self.assertEqual(self.get(covering, url).status_code, 200)


class NonAdminFieldAccessTests(TestCase):
    """Field Access over address and email holds for a reader without the key.

    The gate admits every member of staff; what each sees of a branch is still
    their role's switches. Lagoon View's Teacher role has both closed, its
    Finance Admin role has only email closed.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        install_declared_fields("school.branches")

        cls.school = make_school(slug="lagoon-view", name="Lagoon View")
        cls.tenant = cls.school.tenant
        cls.ikoyi = make_branch(
            cls.school, name="Ikoyi Branch", is_main=True,
            address="2 Bourdillon Road, Ikoyi", email="ikoyi@lagoon-view.test",
        )
        make_branch(
            cls.school, name="Ajah Branch", is_main=False,
            address="5 Addo Road, Ajah", email="ajah@lagoon-view.test",
        )

        teacher = make_role(cls.school, name="Teacher", key="teacher")
        set_field_access(
            teacher, "school.branches.address", "school.branches.email",
            read=False, write=False,
        )
        finance_admin = make_role(cls.school, name="Finance Admin", key="finance_admin")
        set_field_access(finance_admin, "school.branches.email", read=False, write=False)

        cls.tunde = make_school_admin(
            None, email="tunde@lagoon-view.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.tunde, teacher, branch=None)
        cls.ngozi = make_school_admin(
            None, email="ngozi@lagoon-view.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.ngozi, finance_admin, branch=None)

    def rows(self, user):
        response = TenantAPIClient(user=user).get(reverse("my-branch-list"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_a_teacher_with_both_switches_closed_reads_names_only(self):
        for row in self.rows(self.tunde):
            self.assertIn("name", row)
            self.assertNotIn("address", row)
            self.assertNotIn("email", row)

    def test_the_finance_admin_reads_the_address_but_not_the_email(self):
        for row in self.rows(self.ngozi):
            self.assertIn("address", row)
            self.assertNotIn("email", row)

    def test_the_detail_obeys_the_same_switches(self):
        response = TenantAPIClient(user=self.tunde).get(
            reverse("my-branch-detail", kwargs={"code": self.ikoyi.code}),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("address", response.data["data"])
        self.assertNotIn("email", response.data["data"])


class PendingSchoolBranchesTests(TestCase):
    """A school still onboarding can read its own branches.

    Shut, the consequence would be an unreachable go-live rather than a broken
    screen: ``TaskKey.ACADEMIC_STRUCTURE`` is a required onboarding task, the
    academic-structure screens scope every row to the whole school or to one
    branch, and that control reads this list. A two-branch school could never
    finish the task that makes it live.
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
        """Only the list is open before go-live, and absence still means closed.

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

        Proves the open surface makes no difference to a live school, which is
        the half of the blast radius the PENDING cases cannot see.
        """
        self.school.status = "ACTIVE"
        self.school.save()
        response = self.client_for(self.admin).get(
            reverse("my-branch-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]), 2)


class BranchClassCountTests(TestCase):
    """``classes_count`` on ``SchoolBranchSerializer``, annotated from a real
    Class model.

    Three rules hold it: live classes only, zero rather than null for a branch
    with none, and the shared classes of a school that has never used the
    branch column belonging to no branch's card.
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
        """No Student and no Teacher model, so a number for either would be invented."""
        row = self.rows()["Lekki Branch"]
        self.assertIsNone(row["students_count"])
        self.assertIsNone(row["teachers_count"])
