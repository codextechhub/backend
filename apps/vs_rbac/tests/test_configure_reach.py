"""Who may change a setting: :func:`vs_rbac.scoping.assert_caller_may_configure`.

A setting with no branch binds every branch of the tenant, so only a caller
whose reach is the whole tenant may change it. A setting keyed to a branch is
that branch's own, and a caller covering the branch may change it. The
permission key is a separate question and is not asked here.

Lagoon View runs Ikeja and Lekki. Ngozi is pinned to Ikeja, Emeka covers both
through a grant naming each, and Funmi holds a whole-tenant grant.
"""
from django.test import TestCase

from vs_rbac.exceptions import SharedRecordReadOnly
from vs_rbac.scoping import assert_caller_may_configure

from .helpers import (
    make_assignment,
    make_branch,
    make_role,
    make_school,
    make_staff_user,
)


class AssertCallerMayConfigureTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="lagoon-view-cfg", name="Lagoon View")
        cls.tenant = cls.school.tenant
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        role = make_role(cls.school, name="Settings Admin")

        cls.ngozi = make_staff_user(cls.ikeja, email="ngozi@lagoon-cfg.test")
        make_assignment(cls.school, cls.ngozi, role, branch=cls.ikeja)

        cls.emeka = make_staff_user(cls.ikeja, email="emeka@lagoon-cfg.test")
        make_assignment(cls.school, cls.emeka, role, branch=cls.ikeja)
        make_assignment(cls.school, cls.emeka, role, branch=cls.lekki)

        cls.funmi = make_staff_user(cls.ikeja, email="funmi@lagoon-cfg.test")
        make_assignment(cls.school, cls.funmi, role, branch=None)

    def test_a_branch_bound_caller_is_refused_the_tenants_own_setting(self):
        for user in (self.ngozi, self.emeka):
            with self.subTest(user=user.email):
                with self.assertRaises(SharedRecordReadOnly) as refused:
                    assert_caller_may_configure(
                        user, self.tenant, message="Only a school-wide administrator can.",
                    )
                self.assertEqual(refused.exception.message, "Only a school-wide administrator can.")
                self.assertEqual(refused.exception.http_status, 403)

    def test_the_refusal_falls_back_to_the_shared_row_sentence(self):
        with self.assertRaises(SharedRecordReadOnly) as refused:
            assert_caller_may_configure(self.ngozi, self.tenant)
        self.assertEqual(refused.exception.message, SharedRecordReadOnly.default_message)

    def test_a_whole_tenant_caller_may_change_the_tenants_setting_and_any_branchs(self):
        assert_caller_may_configure(self.funmi, self.tenant)
        assert_caller_may_configure(self.funmi, self.tenant, self.ikeja)
        assert_caller_may_configure(self.funmi, self.tenant, self.lekki.pk)

    def test_a_branch_bound_caller_may_change_their_own_branchs_setting(self):
        assert_caller_may_configure(self.ngozi, self.tenant, self.ikeja)
        assert_caller_may_configure(self.emeka, self.tenant, self.lekki)

    def test_a_branch_bound_caller_is_refused_another_branchs_setting(self):
        with self.assertRaises(SharedRecordReadOnly):
            assert_caller_may_configure(self.ngozi, self.tenant, self.lekki)


class OneBranchTenantReachTests(TestCase):
    """A grant pinned to a tenant's only branch reaches the whole tenant, everywhere.

    Harbour Primary has one branch, Main, and Tolu's grant is pinned to it.
    Every row there is a Main row or a shared one, so the pin says nothing: she
    reads, writes, grants and approves exactly as an unpinned bursar does. Bright
    Star has Ikeja and Lekki, and Kemi's identical grant pinned to Ikeja narrows
    her. The day Harbour opens a second branch, Tolu is narrowed to Main again
    on the next request.
    """

    KEY = "finance.invoice.view"

    @classmethod
    def setUpTestData(cls):
        from .helpers import make_permission, make_role_permission

        permission = make_permission(cls.KEY)

        cls.harbour = make_school(slug="harbour-cfg", name="Harbour Primary")
        cls.harbour_main = make_branch(cls.harbour, name="Main Branch")
        cls.harbour_role = make_role(cls.harbour, name="Bursar")
        make_role_permission(cls.harbour_role, permission)
        cls.tolu = make_staff_user(cls.harbour_main, email="tolu@harbour-cfg.test")
        make_assignment(cls.harbour, cls.tolu, cls.harbour_role, branch=cls.harbour_main)

        # Holds no grant at all; only her home posting speaks for her.
        cls.posted = make_staff_user(cls.harbour_main, email="posted@harbour-cfg.test")

        # Holds an unpinned grant of a role whose own reach is Main.
        capped = make_role(cls.harbour, name="Main Bursar", branch=cls.harbour_main)
        make_role_permission(capped, permission)
        cls.sade = make_staff_user(cls.harbour_main, email="sade@harbour-cfg.test")
        make_assignment(cls.harbour, cls.sade, capped, branch=None)

        # Posted nowhere, so only a whole-tenant caller may change her roles.
        cls.registrar = make_staff_user(
            None, email="registrar@harbour-cfg.test", tenant=cls.harbour.tenant,
        )

        cls.bright = make_school(slug="bright-star-cfg", name="Bright Star")
        cls.ikeja = make_branch(cls.bright, name="Ikeja Branch")
        cls.lekki = make_branch(cls.bright, name="Lekki Branch", is_main=False)
        bright_role = make_role(cls.bright, name="Bursar")
        make_role_permission(bright_role, permission)
        cls.kemi = make_staff_user(cls.ikeja, email="kemi@bright-cfg.test")
        make_assignment(cls.bright, cls.kemi, bright_role, branch=cls.ikeja)

    @staticmethod
    def fresh(user):
        """The user as the next request loads them, with nothing memoised."""
        from django.contrib.auth import get_user_model

        return get_user_model().objects.get(pk=user.pk)

    def open_second_branch(self):
        return make_branch(self.harbour, name="Ajah Branch", is_main=False)

    @staticmethod
    def request_as(user):
        from types import SimpleNamespace

        return SimpleNamespace(user=user)

    # -- one answer --------------------------------------------------------- #

    def test_a_grant_pinned_to_the_only_branch_may_change_the_tenants_setting(self):
        from vs_rbac.scoping import (
            branch_reach_payload,
            caller_may_change,
            caller_reaches_whole_tenant,
        )

        tolu, tenant = self.fresh(self.tolu), self.harbour.tenant
        assert_caller_may_configure(tolu, tenant)
        self.assertTrue(caller_reaches_whole_tenant(tolu, tenant))
        self.assertTrue(caller_may_change(tolu, tenant, ()))
        self.assertEqual(
            branch_reach_payload(tolu, tenant), {"whole_tenant": True, "branch_ids": []},
        )

    def test_her_reads_are_whole_tenant_too(self):
        from vs_rbac.scoping import WHOLE_TENANT, branch_scope_for_user, visible_branch_ids

        tolu, tenant = self.fresh(self.tolu), self.harbour.tenant
        self.assertIs(visible_branch_ids(tolu, tenant), WHOLE_TENANT)
        self.assertFalse(branch_scope_for_user(tolu, tenant=tenant).is_narrowed)
        # Procurement's exclusive reading, which hides tenant-wide spend from a
        # branch-bound reader, does not hide it from her.
        self.assertFalse(
            branch_scope_for_user(tolu, tenant=tenant, include_shared=False).is_narrowed,
        )

    def test_a_home_posting_at_the_only_branch_reaches_the_whole_tenant(self):
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

        self.assertIs(
            visible_branch_ids(self.fresh(self.posted), self.harbour.tenant), WHOLE_TENANT,
        )

    def test_a_role_capped_to_the_only_branch_reaches_the_whole_tenant(self):
        from vs_rbac.evaluator import has_permission
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

        sade, tenant = self.fresh(self.sade), self.harbour.tenant
        self.assertIs(visible_branch_ids(sade, tenant), WHOLE_TENANT)
        self.assertTrue(has_permission(sade, self.KEY, tenant=tenant, branch=None))

    def test_the_bulk_reader_gives_the_same_answer(self):
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids_for

        self.assertEqual(
            visible_branch_ids_for([self.tolu, self.posted], self.harbour.tenant),
            {self.tolu.pk: WHOLE_TENANT, self.posted.pk: WHOLE_TENANT},
        )
        self.assertEqual(
            visible_branch_ids_for([self.kemi], self.bright.tenant),
            {self.kemi.pk: frozenset({self.ikeja.pk})},
        )

    # -- granting, approving and filing ------------------------------------- #

    def test_she_may_grant_across_the_whole_tenant(self):
        from vs_rbac.grant_reach import assert_caller_may_grant

        assert_caller_may_grant(
            self.fresh(self.tolu), self.harbour.tenant, self.harbour_role, None,
            holder=self.registrar,
        )

    def test_she_holds_and_is_routed_work_for_the_whole_tenant(self):
        from vs_rbac.evaluator import has_permission, resolve_users_with_permission

        tolu, tenant = self.fresh(self.tolu), self.harbour.tenant
        self.assertTrue(has_permission(tolu, self.KEY, tenant=tenant, branch=None))
        self.assertIn(tolu, resolve_users_with_permission(tenant, None, self.KEY))

    def test_a_row_she_raises_without_naming_a_branch_is_tenant_wide(self):
        from vs_rbac.scoping import raised_branch

        request, tenant = self.request_as(self.fresh(self.tolu)), self.harbour.tenant
        self.assertIsNone(raised_branch(request, tenant, {}))
        self.assertEqual(
            raised_branch(request, tenant, {"branch": self.harbour_main.pk}),
            self.harbour_main,
        )

    def test_she_may_continue_a_tenant_wide_chain(self):
        from types import SimpleNamespace

        from vs_rbac.scoping import inherited_branch_id

        request = self.request_as(self.fresh(self.tolu))
        self.assertIsNone(inherited_branch_id(request, SimpleNamespace(branch_id=None)))

    # -- two branches ------------------------------------------------------- #

    def test_a_grant_pinned_to_one_of_two_branches_is_still_refused(self):
        from vs_rbac.evaluator import has_permission, resolve_users_with_permission
        from vs_rbac.scoping import branch_reach_payload, branch_scope_for_user

        kemi, tenant = self.fresh(self.kemi), self.bright.tenant
        with self.assertRaises(SharedRecordReadOnly):
            assert_caller_may_configure(kemi, tenant)
        self.assertEqual(
            branch_reach_payload(kemi, tenant),
            {"whole_tenant": False, "branch_ids": [self.ikeja.pk]},
        )
        self.assertTrue(branch_scope_for_user(kemi, tenant=tenant).is_narrowed)
        self.assertFalse(has_permission(kemi, self.KEY, tenant=tenant, branch=None))
        self.assertNotIn(kemi, resolve_users_with_permission(tenant, None, self.KEY))

    def test_a_second_branch_makes_the_pinned_grant_branch_bound_again(self):
        from vs_rbac.scoping import branch_reach_payload

        tenant = self.harbour.tenant
        assert_caller_may_configure(self.fresh(self.tolu), tenant)

        self.open_second_branch()

        tolu = self.fresh(self.tolu)
        with self.assertRaises(SharedRecordReadOnly):
            assert_caller_may_configure(tolu, tenant)
        self.assertEqual(
            branch_reach_payload(tolu, tenant),
            {"whole_tenant": False, "branch_ids": [self.harbour_main.pk]},
        )
        # Her own branch's setting is still hers.
        assert_caller_may_configure(tolu, tenant, self.harbour_main)

    def test_a_second_branch_narrows_her_reads_again(self):
        from vs_rbac.scoping import branch_scope_for_user, visible_branch_ids

        tenant = self.harbour.tenant
        self.assertFalse(branch_scope_for_user(self.fresh(self.tolu), tenant=tenant).is_narrowed)

        self.open_second_branch()

        tolu = self.fresh(self.tolu)
        self.assertEqual(visible_branch_ids(tolu, tenant), frozenset({self.harbour_main.pk}))
        self.assertTrue(branch_scope_for_user(tolu, tenant=tenant).is_narrowed)
        self.assertEqual(
            visible_branch_ids(self.fresh(self.posted), tenant),
            frozenset({self.harbour_main.pk}),
        )

    def test_a_second_branch_withdraws_her_tenant_wide_grants_and_approvals(self):
        from rest_framework.exceptions import ValidationError

        from vs_rbac.evaluator import has_permission, resolve_users_with_permission
        from vs_rbac.grant_reach import assert_caller_may_grant

        tenant = self.harbour.tenant
        self.open_second_branch()

        tolu = self.fresh(self.tolu)
        with self.assertRaises(ValidationError):
            assert_caller_may_grant(tolu, tenant, self.harbour_role, None, holder=self.registrar)
        self.assertFalse(has_permission(tolu, self.KEY, tenant=tenant, branch=None))
        self.assertNotIn(tolu, resolve_users_with_permission(tenant, None, self.KEY))
        # Her own branch's work still reaches her.
        self.assertTrue(has_permission(tolu, self.KEY, tenant=tenant, branch=self.harbour_main))

    def test_a_second_branch_files_her_rows_under_her_branch_again(self):
        from types import SimpleNamespace

        from rest_framework.exceptions import PermissionDenied

        from vs_rbac.scoping import inherited_branch_id, raised_branch

        tenant = self.harbour.tenant
        ajah = self.open_second_branch()

        request = self.request_as(self.fresh(self.tolu))
        self.assertEqual(raised_branch(request, tenant, {}), self.harbour_main)
        with self.assertRaises(PermissionDenied):
            raised_branch(request, tenant, {"branch": ajah.pk})
        with self.assertRaises(PermissionDenied):
            inherited_branch_id(request, SimpleNamespace(branch_id=None))

    # -- cost --------------------------------------------------------------- #

    def test_a_list_asks_the_branch_count_once(self):
        from vs_rbac.scoping import caller_may_change, visible_branch_ids

        tolu, tenant = self.fresh(self.tolu), self.harbour.tenant
        visible = visible_branch_ids(tolu, tenant)
        caller_may_change(tolu, tenant, (), visible=visible)
        with self.assertNumQueries(0):
            for _ in range(5):
                self.assertTrue(caller_may_change(tolu, tenant, (), visible=visible))

    def test_the_rule_costs_no_query_of_its_own(self):
        """The grants query brings the branch count; the posting query does too."""
        from vs_rbac.scoping import visible_branch_ids

        for user, tenant in (
            (self.fresh(self.kemi), self.bright.tenant),
            (self.fresh(self.tolu), self.harbour.tenant),
        ):
            with self.subTest(user=user.email):
                with self.assertNumQueries(1):
                    visible_branch_ids(user, tenant)
                with self.assertNumQueries(0):
                    visible_branch_ids(user, tenant)
        posted = self.fresh(self.posted)
        with self.assertNumQueries(2):
            visible_branch_ids(posted, self.harbour.tenant)
