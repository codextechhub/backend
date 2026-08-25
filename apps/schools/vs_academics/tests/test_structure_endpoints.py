"""Departments, programmes and levels over HTTP.

Security first, then the two rules that are this module's own: a child may be
no wider than its parent, and a branch-bound caller may not speak for the whole
school.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

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
from vs_user.tokens import CodeXRefreshToken
from schools.vs_academics.models import Department, Level, Program

KEYS = (
    "academics.structure.view",
    "academics.structure.create",
    "academics.structure.update",
    "academics.structure.manage",
)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        for key in KEYS:
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)

        # A head of the Lekki campus, whose grant is pinned to that branch.
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, cls.role, branch=cls.lekki)

        cls.other = make_school(slug="sunrise", name="Sunrise Academy")
        make_branch(cls.other, name="Main", is_main=True)

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def post(self, user, name, body, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).post(
            f"{url}?tenant={self.tenant.slug}", body, format="json",
        )

    def get(self, user, name, params=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).get(
            url, {"tenant": self.tenant.slug, **(params or {})},
        )

    def dept(self, name="Sciences", code="SCI", branch=None, tenant=None):
        return Department.all_objects.create(
            tenant=tenant or self.tenant, name=name, code=code, branch=branch,
        )

    def program(self, name="Junior Secondary", code="JSS", branch=None, dept=None):
        return Program.all_objects.create(
            tenant=self.tenant, name=name, code=code, branch=branch,
            department=dept,
        )


class SecurityTests(_Base):
    def test_a_caller_without_the_key_is_refused(self):
        stranger = make_school_admin(
            None, email="nobody@brightfield.test", tenant=self.tenant,
        )
        response = self.get(stranger, "academics-department-list")
        self.assertEqual(response.status_code, 403, response.data)

    def test_another_schools_department_is_a_404(self):
        theirs = self.dept(tenant=self.other.tenant)
        response = self.get(
            self.admin, "academics-department-detail", pk=theirs.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_branch_bound_caller_cannot_read_another_branchs_row(self):
        theirs = self.dept("Ikeja Only", "IKO", branch=self.ikeja)
        response = self.get(
            self.lekki_head, "academics-department-detail", pk=theirs.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_an_omitted_branch_is_filled_in_from_the_callers_own(self):
        """Omitting the field means "wherever I work", not "the whole school"."""
        response = self.post(
            self.lekki_head, "academics-department-list",
            {"name": "Sciences", "code": "SCI"},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Department.all_objects.get().branch_id, self.lekki.id)

    def test_a_branch_bound_caller_may_not_create_a_shared_row(self):
        """Choosing "the whole school" by name is a different act from omitting.

        The design offers it as a radio, so the API has to be able to receive
        it and refuse it. 403 rather than 422 on purpose: it is about who the
        caller is, not about what they typed.
        """
        response = self.post(
            self.lekki_head, "academics-department-list",
            {"name": "Sciences", "code": "SCI", "branch": None},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Department.all_objects.count(), 0)

    def test_a_school_level_caller_may_create_a_shared_row_explicitly(self):
        response = self.post(
            self.admin, "academics-department-list",
            {"name": "Sciences", "code": "SCI", "branch": None},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(Department.all_objects.get().branch_id)

    def test_a_branch_bound_caller_cannot_create_in_another_branch(self):
        response = self.post(
            self.lekki_head, "academics-department-list",
            {"name": "Sciences", "code": "SCI", "branch": self.ikeja.id},
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Department.all_objects.count(), 0)


class VisibilityTests(_Base):
    def test_a_branch_caller_sees_the_shared_rows_plus_their_own(self):
        """The inclusive read. Excluding shared rows would empty the screen."""
        self.dept("Sciences", "SCI")
        self.dept("Lekki Extra", "LEX", branch=self.lekki)
        self.dept("Ikeja Extra", "IEX", branch=self.ikeja)

        response = self.get(self.lekki_head, "academics-department-list")
        names = {row["name"] for row in response.data["data"]}
        self.assertEqual(names, {"Sciences", "Lekki Extra"})

    def test_a_school_level_caller_sees_everything(self):
        self.dept("Sciences", "SCI")
        self.dept("Lekki Extra", "LEX", branch=self.lekki)
        self.dept("Ikeja Extra", "IEX", branch=self.ikeja)

        response = self.get(self.admin, "academics-department-list")
        self.assertEqual(len(response.data["data"]), 3)

    def test_the_scope_filter_narrows_to_shared_rows(self):
        self.dept("Sciences", "SCI")
        self.dept("Lekki Extra", "LEX", branch=self.lekki)
        response = self.get(
            self.admin, "academics-department-list", {"branch": "none"},
        )
        self.assertEqual([r["name"] for r in response.data["data"]], ["Sciences"])

    def test_an_empty_list_is_still_a_list(self):
        response = self.get(self.admin, "academics-department-list")
        self.assertEqual(response.data["data"], [])


class DepartmentTests(_Base):
    def test_a_code_is_generated_when_omitted(self):
        response = self.post(
            self.admin, "academics-department-list", {"name": "Sciences"},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["code"], "SCI")

    def test_a_generated_code_is_suffixed_rather_than_clashing(self):
        self.post(self.admin, "academics-department-list", {"name": "Sciences"})
        response = self.post(
            self.admin, "academics-department-list", {"name": "Scientific Studies"},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["code"], "SCI2")

    def test_the_program_count_is_real(self):
        dept = self.dept()
        self.program(dept=dept)
        self.program("Senior Secondary", "SSS", dept=dept)
        response = self.get(self.admin, "academics-department-list")
        self.assertEqual(response.data["data"][0]["program_count"], 2)

    def test_deleting_a_department_with_programmes_is_refused(self):
        """Reverses what FRD 2.0 to 2.5.1 promised, because the design does."""
        dept = self.dept()
        self.program(dept=dept)
        url = reverse("academics-department-detail", kwargs={"pk": dept.pk})
        response = self.client_for(self.admin).delete(
            f"{url}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "PROTECTED_REFERENCE")
        self.assertEqual(response.data["error"]["detail"], {"Program": 1})
        self.assertTrue(Department.all_objects.filter(pk=dept.pk).exists())

    def test_a_department_with_only_subjects_still_deletes(self):
        """The asymmetry is deliberate, so it is asserted rather than assumed.

        The design refuses on programmes and shows no subject count at all, so
        subjects still detach. FRD v2.6 decision 4 keeps this half open.
        """
        from schools.vs_academics.models import Subject

        dept = self.dept()
        subject = Subject.all_objects.create(
            tenant=self.tenant, name="Mathematics", code="MTH", department=dept,
        )
        url = reverse("academics-department-detail", kwargs={"pk": dept.pk})
        response = self.client_for(self.admin).delete(
            f"{url}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        subject.refresh_from_db()
        self.assertIsNone(subject.department_id)


class ContainmentTests(_Base):
    """A child may be no wider than its parent."""

    def test_a_shared_programme_under_a_branch_department_is_refused(self):
        dept = self.dept("Lekki Sciences", "LSC", branch=self.lekki)
        response = self.post(
            self.admin, "academics-program-list",
            {"name": "Junior Secondary", "code": "JSS", "department": dept.pk},
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "BRANCH_SCOPE_CONFLICT")

    def test_a_branch_programme_under_a_shared_department_succeeds(self):
        dept = self.dept()
        response = self.post(
            self.admin, "academics-program-list",
            {"name": "Junior Secondary", "code": "JSS",
             "department": dept.pk, "branch": self.lekki.id},
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_shared_level_under_a_branch_programme_is_refused(self):
        program = self.program(branch=self.lekki)
        response = self.post(
            self.admin, "academics-level-list", {"name": "JSS1"}, pk=program.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "BRANCH_SCOPE_CONFLICT")

    def test_a_level_in_a_different_branch_from_its_programme_is_refused(self):
        program = self.program(branch=self.lekki)
        response = self.post(
            self.admin, "academics-level-list",
            {"name": "JSS1", "branch": self.ikeja.id}, pk=program.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)


class LevelTests(_Base):
    def setUp(self):
        self.prog = self.program()

    def test_order_index_follows_the_highest_so_far(self):
        for name in ("JSS1", "JSS2"):
            self.post(
                self.admin, "academics-level-list", {"name": name}, pk=self.prog.pk,
            )
        self.assertEqual(
            list(Level.all_objects.order_by("order_index")
                 .values_list("name", "order_index")),
            [("JSS1", 1), ("JSS2", 2)],
        )

    def test_bulk_creates_a_run_in_order(self):
        response = self.post(
            self.admin, "academics-level-bulk",
            {"names": ["JSS1", "JSS2", "JSS3"]}, pk=self.prog.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [r["name"] for r in response.data["data"]], ["JSS1", "JSS2", "JSS3"],
        )
        self.assertEqual([r["order_index"] for r in response.data["data"]], [1, 2, 3])

    def test_one_duplicate_creates_nothing_at_all(self):
        self.post(
            self.admin, "academics-level-bulk", {"names": ["JSS1"]}, pk=self.prog.pk,
        )
        response = self.post(
            self.admin, "academics-level-bulk",
            {"names": ["JSS2", "JSS1", "JSS3"]}, pk=self.prog.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_IN_BATCH")
        self.assertEqual(response.data["error"]["detail"]["names"], ["JSS1"])
        self.assertEqual(Level.all_objects.count(), 1)

    def test_a_duplicate_inside_the_batch_is_caught_too(self):
        response = self.post(
            self.admin, "academics-level-bulk",
            {"names": ["JSS1", "JSS1"]}, pk=self.prog.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(Level.all_objects.count(), 0)

    def test_deleting_a_programme_with_levels_is_refused(self):
        self.post(self.admin, "academics-level-list", {"name": "JSS1"}, pk=self.prog.pk)
        url = reverse("academics-program-detail", kwargs={"pk": self.prog.pk})
        response = self.client_for(self.admin).delete(
            f"{url}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "PROTECTED_REFERENCE")
        self.assertTrue(Program.all_objects.filter(pk=self.prog.pk).exists())

    def test_the_programme_list_nests_its_levels(self):
        self.post(
            self.admin, "academics-level-bulk",
            {"names": ["JSS1", "JSS2"]}, pk=self.prog.pk,
        )
        response = self.get(self.admin, "academics-program-list")
        row = response.data["data"][0]
        self.assertEqual(row["level_count"], 2)
        self.assertEqual([lv["name"] for lv in row["levels"]], ["JSS1", "JSS2"])


class PromotionTests(_Base):
    def setUp(self):
        self.prog = self.program()
        self.a, self.b, self.c = (
            Level.all_objects.create(
                tenant=self.tenant, program=self.prog, name=n, code=n, order_index=i,
            )
            for i, n in enumerate(("JSS1", "JSS2", "JSS3"), start=1)
        )

    def patch_level(self, level, body):
        url = reverse("academics-level-detail", kwargs={"pk": level.pk})
        return self.client_for(self.admin).patch(
            f"{url}?tenant={self.tenant.slug}", body, format="json",
        )

    def test_a_chain_can_be_wired(self):
        self.assertEqual(self.patch_level(self.a, {"next_level": self.b.pk}).status_code, 200)
        self.assertEqual(self.patch_level(self.b, {"next_level": self.c.pk}).status_code, 200)

    def test_a_loop_is_refused_and_names_both_ends(self):
        self.patch_level(self.a, {"next_level": self.b.pk})
        self.patch_level(self.b, {"next_level": self.c.pk})
        response = self.patch_level(self.c, {"next_level": self.a.pk})
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "LEVEL_CYCLE")
        self.assertEqual(response.data["error"]["detail"]["level"], "JSS3")
        self.assertEqual(response.data["error"]["detail"]["target"], "JSS1")

    def test_a_level_cannot_promote_into_itself(self):
        response = self.patch_level(self.a, {"next_level": self.a.pk})
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "LEVEL_CYCLE")

    def test_another_programme_needs_cross_program(self):
        other = self.program("Senior Secondary", "SSS")
        target = Level.all_objects.create(
            tenant=self.tenant, program=other, name="SSS1", code="SSS1", order_index=1,
        )
        response = self.patch_level(self.c, {"next_level": target.pk})
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "LEVEL_CROSS_PROGRAM")

        url = reverse("academics-level-detail", kwargs={"pk": self.c.pk})
        allowed = self.client_for(self.admin).patch(
            f"{url}?tenant={self.tenant.slug}&cross_program=true",
            {"next_level": target.pk}, format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_a_shared_level_may_not_promote_into_one_branchs_level(self):
        """It would push every branch's pupils to one site."""
        branch_level = Level.all_objects.create(
            tenant=self.tenant, program=self.prog, name="Lekki Extra",
            code="LEX", order_index=9, branch=self.lekki,
        )
        response = self.patch_level(self.c, {"next_level": branch_level.pk})
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "BRANCH_SCOPE_CONFLICT")

    def test_another_tenants_level_is_not_a_valid_target(self):
        other_prog = Program.all_objects.create(
            tenant=self.other.tenant, name="Theirs", code="THR",
        )
        theirs = Level.all_objects.create(
            tenant=self.other.tenant, program=other_prog, name="JSS2",
            code="JSS2", order_index=1,
        )
        response = self.patch_level(self.a, {"next_level": theirs.pk})
        self.assertEqual(response.status_code, 400, response.data)
        self.a.refresh_from_db()
        self.assertIsNone(self.a.next_level_id)
