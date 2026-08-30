"""Departments, programmes and levels over HTTP.

Security first, then the two rules that are this module's own: a child may be
no wider than its parent, and a branch-bound caller may not speak for the whole
school.
"""
from __future__ import annotations

import datetime as dt

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
from schools.vs_academics.models import (
    AcademicSession,
    Department,
    Level,
    Program,
)

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
        # The other school needs a year of its own: a level belongs to one, and
        # a year belongs to one school.
        cls.other_year = AcademicSession.all_objects.create(
            tenant=cls.other.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status="ACTIVE",
        )
        # Levels, classes and subjects belong to a year now, so the
        # fixtures need one to put them in.
        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status="ACTIVE",
        )

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

    def test_the_program_count_is_the_years_not_all_time(self):
        """A department card describes the year being read, like everything else.

        A programme spans every year, so what makes it part of THIS one is
        having a level in it. Two programmes mapped to Sciences but running
        nothing this year is a Sciences that is running nothing this year.
        """
        dept = self.dept()
        jss = self.program(dept=dept)
        self.program("Senior Secondary", "SSS", dept=dept)

        response = self.get(self.admin, "academics-department-list")
        self.assertEqual(response.data["data"][0]["program_count"], 0)

        Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss,
            name="JSS1", code="JSS1", order_index=1,
        )
        response = self.get(self.admin, "academics-department-list")
        self.assertEqual(response.data["data"][0]["program_count"], 1)

    def test_there_is_no_delete_route_for_a_department(self):
        dept = self.dept()
        url = reverse("academics-department-detail", kwargs={"pk": dept.pk})
        response = self.client_for(self.admin).delete(
            f"{url}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 405, response.data)
        self.assertTrue(Department.all_objects.filter(pk=dept.pk).exists())

    def test_archiving_a_department_leaves_its_programmes_where_they_are(self):
        """Archiving says one thing and does one thing.

        A delete had to be refused while programmes pointed at it. Archiving
        does not, because it is reversible and takes nothing with it - and
        cascading it would be the school saying "retire Sciences" and the
        platform hearing "retire everything taught under it".
        """
        dept = self.dept()
        program = self.program(dept=dept)

        response = self.post(
            self.admin, "academics-department-archive", {}, pk=dept.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        dept.refresh_from_db()
        program.refresh_from_db()
        self.assertFalse(dept.is_active)
        self.assertTrue(program.is_active)
        self.assertEqual(program.department_id, dept.pk)

    def test_an_archived_department_comes_back(self):
        dept = self.dept()
        self.post(self.admin, "academics-department-archive", {}, pk=dept.pk)
        response = self.post(
            self.admin, "academics-department-restore", {}, pk=dept.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        dept.refresh_from_db()
        self.assertTrue(dept.is_active)


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

    def test_there_is_no_delete_route_for_a_programme_or_a_level(self):
        self.post(self.admin, "academics-level-list", {"name": "JSS1"}, pk=self.prog.pk)
        level = Level.all_objects.get()
        for name, pk in (
            ("academics-program-detail", self.prog.pk),
            ("academics-level-detail", level.pk),
        ):
            url = reverse(name, kwargs={"pk": pk})
            response = self.client_for(self.admin).delete(
                f"{url}?tenant={self.tenant.slug}",
            )
            self.assertEqual(response.status_code, 405, name)
        self.assertTrue(Program.all_objects.filter(pk=self.prog.pk).exists())
        self.assertTrue(Level.all_objects.filter(pk=level.pk).exists())

    def test_archiving_a_programme_leaves_its_levels_where_they_are(self):
        self.post(self.admin, "academics-level-list", {"name": "JSS1"}, pk=self.prog.pk)
        response = self.post(
            self.admin, "academics-program-archive", {}, pk=self.prog.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.prog.refresh_from_db()
        self.assertFalse(self.prog.is_active)
        self.assertTrue(Level.all_objects.get().is_active)

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
                tenant=self.tenant, session=self.year, program=self.prog, name=n, code=n, order_index=i,
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
            tenant=self.tenant, session=self.year, program=other, name="SSS1", code="SSS1", order_index=1,
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
            tenant=self.tenant, session=self.year, program=self.prog, name="Lekki Extra",
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
            tenant=self.other.tenant, session=self.other_year, program=other_prog, name="JSS2",
            code="JSS2", order_index=1,
        )
        response = self.patch_level(self.a, {"next_level": theirs.pk})
        self.assertEqual(response.status_code, 400, response.data)
        self.a.refresh_from_db()
        self.assertIsNone(self.a.next_level_id)


class PromotionScreenTests(_Base):
    """What a screen needs to render and edit the promotion chain.

    The API carried next_level as an id from the start, which is enough to
    write with and not enough to draw with: a level detail read would have
    needed a second call just to name what it promotes to.
    """

    def setUp(self):
        self.prog = self.program()
        self.jss1, self.jss2 = (
            Level.all_objects.create(
                tenant=self.tenant, session=self.year, program=self.prog, name=n, code=n, order_index=i,
            )
            for i, n in enumerate(("JSS1", "JSS2"), start=1)
        )

    def test_a_level_names_what_it_promotes_to(self):
        self.jss1.next_level = self.jss2
        self.jss1.save(update_fields=["next_level"])
        response = self.get(
            self.admin, "academics-level-detail", pk=self.jss1.pk,
        )
        self.assertEqual(response.data["data"]["next_level"], self.jss2.pk)
        self.assertEqual(response.data["data"]["next_level_name"], "JSS2")

    def test_a_terminal_level_names_nothing(self):
        """Null here means terminal OR not yet wired; FR-005 says why both."""
        response = self.get(
            self.admin, "academics-level-detail", pk=self.jss2.pk,
        )
        self.assertIsNone(response.data["data"]["next_level_name"])

    def test_the_programme_list_draws_the_whole_chain_in_one_call(self):
        self.jss1.next_level = self.jss2
        self.jss1.save(update_fields=["next_level"])
        response = self.get(self.admin, "academics-program-list")
        levels = response.data["data"][0]["levels"]
        self.assertEqual(
            [(lv["name"], lv["next_level_name"]) for lv in levels],
            [("JSS1", "JSS2"), ("JSS2", None)],
        )

    def test_naming_the_target_costs_no_extra_query_per_level(self):
        """The join is what makes the promotion screen affordable.

        Without select_related this is one query per level, on the one screen
        that lists every level a programme has. Asserted by growing the
        programme and requiring the count not to move - comparing a count to
        itself would pass whether or not the join is there.
        """
        client = self.client_for(self.admin)
        url = reverse("academics-program-list")
        params = {"tenant": self.tenant.slug}
        client.get(url, params)                         # warm the auth caches

        # Fifteen: thirteen, plus one to resolve which year the screen is
        # about - paid once per request rather than once per level - plus the
        # RBAC registry-revision read that guards the permission memo. Both are
        # per request and neither grows with the programme, which is what the
        # second assertion below proves.
        with self.assertNumQueries(15) as small:
            client.get(url, params)
        baseline = len(small.captured_queries)

        for i in range(3, 12):
            Level.all_objects.create(
                tenant=self.tenant, session=self.year, program=self.prog, name=f"JSS{i}",
                code=f"JSS{i}", order_index=i, next_level=self.jss2,
            )
        with self.assertNumQueries(baseline):
            response = client.get(url, params)
        # And the extra levels really are in the payload being priced.
        self.assertEqual(len(response.data["data"][0]["levels"]), 11)


class ArchivedRowsAreReachableTests(_Base):
    """An archive nobody can find again is a delete with extra steps.

    Both halves matter: the programme has to come back under `is_active=all`,
    and its LEVELS have to follow the same reading. They did not - the levels
    were nested through a prefetch of their own that ignored the filter, so an
    archived level went on showing inside an active programme while an archived
    programme vanished with its active levels.
    """

    def setUp(self):
        self.prog = self.program()
        self.level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.prog,
            name="JSS1", code="JSS1", order_index=1,
        )

    def names(self, params=None):
        response = self.get(self.admin, "academics-program-list", params)
        self.assertEqual(response.status_code, 200, response.data)
        return {
            row["name"]: [lvl["name"] for lvl in row["levels"]]
            for row in response.data["data"]
        }

    def test_an_archived_programme_is_found_again_and_restored(self):
        self.post(self.admin, "academics-program-archive", {}, pk=self.prog.pk)
        self.assertNotIn("Junior Secondary", self.names())
        self.assertIn("Junior Secondary", self.names({"is_active": "all"}))
        self.assertIn("Junior Secondary", self.names({"is_active": "false"}))

        response = self.post(
            self.admin, "academics-program-restore", {}, pk=self.prog.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("Junior Secondary", self.names())

    def test_an_archived_level_hides_with_the_rest(self):
        self.post(self.admin, "academics-level-archive", {}, pk=self.level.pk)
        self.assertEqual(self.names()["Junior Secondary"], [])
        self.assertEqual(
            self.names({"is_active": "all"})["Junior Secondary"], ["JSS1"],
        )

        self.post(self.admin, "academics-level-restore", {}, pk=self.level.pk)
        self.assertEqual(self.names()["Junior Secondary"], ["JSS1"])


class ALevelSaysWhetherItEndsTests(_Base):
    """The three promotion states, and why two of them used to be one.

    `next_level` alone has two states and the school needs three. A level with
    no target is either the end of the road or a level nobody has wired yet,
    and reading the second as the first is what graduates a year group by
    accident - so `is_terminal` separates them and this pins all three.
    """

    def setUp(self):
        self.prog = self.program()
        self.jss1, self.jss2 = (
            Level.all_objects.create(
                tenant=self.tenant, session=self.year, program=self.prog,
                name=name, code=name, order_index=i,
            )
            for i, name in enumerate(("JSS1", "JSS2"), start=1)
        )

    def patch(self, level, body):
        url = reverse("academics-level-detail", kwargs={"pk": level.pk})
        return self.client_for(self.admin).patch(
            f"{url}?tenant={self.tenant.slug}", body, format="json",
        )

    def state(self, level):
        response = self.get(
            self.admin, "academics-level-detail", pk=level.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["promotion"]

    def test_a_new_level_is_unset_not_terminal(self):
        """The distinction the whole field exists for."""
        self.assertEqual(self.state(self.jss1), "unset")
        self.assertFalse(self.jss1.is_terminal)

    def test_naming_a_target_reads_as_promotes(self):
        response = self.patch(self.jss1, {"next_level": self.jss2.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["promotion"], "promotes")
        self.assertEqual(response.data["data"]["next_level_name"], "JSS2")

    def test_marking_it_terminal_reads_as_terminal(self):
        response = self.patch(self.jss2, {"is_terminal": True})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["promotion"], "terminal")
        self.assertIsNone(response.data["data"]["next_level"])

    def test_naming_a_target_stops_it_being_terminal(self):
        """One named, so the other follows rather than being refused."""
        self.patch(self.jss1, {"is_terminal": True})
        response = self.patch(self.jss1, {"next_level": self.jss2.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.jss1.refresh_from_db()
        self.assertFalse(self.jss1.is_terminal)
        self.assertEqual(self.jss1.next_level_id, self.jss2.pk)

    def test_marking_it_terminal_clears_the_target(self):
        self.patch(self.jss1, {"next_level": self.jss2.pk})
        response = self.patch(self.jss1, {"is_terminal": True})
        self.assertEqual(response.status_code, 200, response.data)
        self.jss1.refresh_from_db()
        self.assertTrue(self.jss1.is_terminal)
        self.assertIsNone(self.jss1.next_level_id)

    def test_naming_both_at_once_is_refused_in_words(self):
        """Sent together they contradict, and the row would fail a constraint.

        A database error is not a sentence anybody can act on, so this is
        refused before it gets there.
        """
        response = self.patch(
            self.jss1, {"next_level": self.jss2.pk, "is_terminal": True},
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.jss1.refresh_from_db()
        self.assertIsNone(self.jss1.next_level_id)
        self.assertFalse(self.jss1.is_terminal)

    def test_the_database_refuses_the_pair_too(self):
        """The service refusal is the path; the constraint is the guarantee."""
        from django.db import IntegrityError, transaction

        self.jss1.next_level = self.jss2
        self.jss1.is_terminal = True
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.jss1.save()

    def test_the_programme_list_carries_the_state_for_every_level(self):
        """The screen reads it here: one call for a whole chain."""
        self.patch(self.jss1, {"next_level": self.jss2.pk})
        self.patch(self.jss2, {"is_terminal": True})
        response = self.get(self.admin, "academics-program-list")
        levels = response.data["data"][0]["levels"]
        self.assertEqual(
            [(lvl["name"], lvl["promotion"]) for lvl in levels],
            [("JSS1", "promotes"), ("JSS2", "terminal")],
        )
