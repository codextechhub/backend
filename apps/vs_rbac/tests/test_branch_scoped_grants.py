"""A role granted for one branch grants that branch - not nothing, not everything.

Before this, ``HasRBACPermission`` read ``request.branch``, an attribute nothing
ever set, so the evaluator was always asked the narrow "entity as a whole"
question and every branch-pinned assignment was discarded. The holder of
"Bursar at Ikeja" was not restricted to Ikeja; they were locked out of the
product entirely, while the branch column sat in the database being displayed
back to administrators as though it meant something.

These tests pin both halves of the answer that replaces it: which permissions a
branch grant confers (:mod:`vs_rbac.evaluator`) and which branches its holder
may then see (:mod:`vs_rbac.scoping`). The end-to-end evidence - that Mrs
Adebayo can open the purchases screen and sees only Ikeja's rows - lives in
``vs_procurement.tests.ProcurementBranchGrantAcceptanceTests``.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from vs_rbac.evaluator import ANY_BRANCH, get_effective_permissions, has_permission
from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
from vs_tenants.models import BranchStatus

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_staff_user,
)

BURSAR_KEY = "procurement.purchase_order.view"


class _BranchGrantFixture(TestCase):
    """One multi-branch tenant, one branchless tenant, and a rival.

    Three branches, not two: the third is what proves a grant of two does not
    quietly become a grant of all. The rival tenant carries an identically named
    branch and role so nothing can pass by matching on a name.
    """

    def setUp(self):
        self.school = make_school(slug="grant-multi", name="Multi Campus")
        self.tenant = self.school.tenant
        self.ikeja = make_branch(self.tenant, name="Ikeja", is_main=True)
        self.lekki = make_branch(self.tenant, name="Lekki", is_main=False)
        self.yaba = make_branch(self.tenant, name="Yaba", is_main=False)

        self.flat_school = make_school(slug="grant-flat", name="Single Site")
        self.flat_tenant = self.flat_school.tenant

        self.rival_school = make_school(slug="grant-rival", name="Rival Group")
        self.rival_tenant = self.rival_school.tenant
        self.rival_ikeja = make_branch(self.rival_tenant, name="Ikeja", is_main=True)

        self.permission = make_permission(BURSAR_KEY)

    def role_granting(self, tenant, name):
        role = make_role(tenant, name=name)
        make_role_permission(role, self.permission, granted=True)
        return role

    def person(self, tenant, email, *, branch=None):
        """A user of ``tenant``; ``branch`` is their legacy home posting."""
        return make_staff_user(
            branch, email=email, tenant=tenant,
            user_type="STAFF" if branch is not None else "SCHOOL_ADMIN",
        )


class BranchScopedGrantAccessTests(_BranchGrantFixture):
    """What a branch-pinned grant lets its holder *do*."""

    def test_branch_grant_confers_the_permission_it_names(self):
        """The defect itself: a grant at Ikeja used to confer nothing anywhere."""
        adebayo = self.person(self.tenant, "adebayo@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)

        self.assertTrue(has_permission(adebayo, BURSAR_KEY, tenant=self.tenant))
        self.assertIn(BURSAR_KEY, get_effective_permissions(adebayo, tenant=self.tenant))

    def test_whole_tenant_grant_still_means_the_whole_tenant(self):
        """Acceptance 3, and the arrangement everyone working today relies on."""
        hq = self.person(self.tenant, "hq@grant.test")
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, hq, role)

        self.assertTrue(has_permission(hq, BURSAR_KEY, tenant=self.tenant))
        self.assertIs(visible_branch_ids(hq, self.tenant), WHOLE_TENANT)

    def test_no_grant_confers_nothing(self):
        stranger = self.person(self.tenant, "stranger@grant.test")
        self.assertFalse(has_permission(stranger, BURSAR_KEY, tenant=self.tenant))

    def test_explicit_none_still_asks_the_entity_wide_question(self):
        """``None`` keeps its meaning; only the *default* changed.

        Routing asks who may act on a document belonging to no branch, and the
        answer must stay "whole-tenant grant holders" - a person pinned to one
        site is not an approver for the entity at large.
        """
        adebayo = self.person(self.tenant, "adebayo-none@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)

        self.assertTrue(has_permission(adebayo, BURSAR_KEY, tenant=self.tenant))
        self.assertFalse(
            has_permission(adebayo, BURSAR_KEY, tenant=self.tenant, branch=None),
        )

    def test_any_branch_and_none_do_not_share_a_cache_entry(self):
        """The two scopes answer differently, so they must key differently.

        ``ANY_BRANCH`` has no ``pk``; folding it through ``getattr(branch, "pk",
        None)`` would have collapsed it onto the ``None`` entry and served one
        question's answer to the other.
        """
        adebayo = self.person(self.tenant, "adebayo-cache@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)

        wide = get_effective_permissions(adebayo, tenant=self.tenant, branch=ANY_BRANCH)
        narrow = get_effective_permissions(adebayo, tenant=self.tenant, branch=None)
        self.assertIn(BURSAR_KEY, wide)
        self.assertEqual(narrow, set())

    def test_a_rival_tenants_branch_grant_reaches_nothing_here(self):
        """Acceptance 5: a branch grant narrows within a tenant, never across one."""
        outsider = self.person(self.rival_tenant, "outsider@grant.test")
        rival_role = self.role_granting(self.rival_tenant, "Bursar")
        make_assignment(self.rival_tenant, outsider, rival_role, branch=self.rival_ikeja)

        self.assertTrue(has_permission(outsider, BURSAR_KEY, tenant=self.rival_tenant))
        self.assertFalse(has_permission(outsider, BURSAR_KEY, tenant=self.tenant))
        self.assertEqual(get_effective_permissions(outsider, tenant=self.tenant), set())


class BranchScopedGrantVisibilityTests(_BranchGrantFixture):
    """Which branches a grant holder may see - the other half of one answer."""

    def test_single_branch_grant_narrows_to_that_branch(self):
        """Acceptance 1, at the source: Mrs Adebayo sees Ikeja and only Ikeja."""
        adebayo = self.person(self.tenant, "adebayo-vis@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)

        self.assertEqual(visible_branch_ids(adebayo, self.tenant), frozenset({self.ikeja.pk}))

    def test_two_branch_grants_show_both_and_only_those_two(self):
        """Acceptance 2, at the source, including the third branch it must exclude."""
        sunday = self.person(self.tenant, "sunday@grant.test")
        ikeja_role = self.role_granting(self.tenant, "Storekeeper Ikeja")
        lekki_role = self.role_granting(self.tenant, "Storekeeper Lekki")
        make_assignment(self.tenant, sunday, ikeja_role, branch=self.ikeja)
        make_assignment(self.tenant, sunday, lekki_role, branch=self.lekki)

        visible = visible_branch_ids(sunday, self.tenant)
        self.assertEqual(visible, frozenset({self.ikeja.pk, self.lekki.pk}))
        self.assertNotIn(self.yaba.pk, visible)
        self.assertIsNot(visible, WHOLE_TENANT)

    def test_the_same_role_can_be_held_at_two_branches(self):
        """The arrangement one ``User.branch`` column cannot express.

        Worth its own test because the schema used to forbid it outright: a
        single unique constraint over (tenant, user, role) meant the second site
        could not be recorded at all.
        """
        sunday = self.person(self.tenant, "sunday-same@grant.test")
        role = self.role_granting(self.tenant, "Storekeeper")
        make_assignment(self.tenant, sunday, role, branch=self.ikeja)
        make_assignment(self.tenant, sunday, role, branch=self.lekki)

        self.assertEqual(
            visible_branch_ids(sunday, self.tenant),
            frozenset({self.ikeja.pk, self.lekki.pk}),
        )

    def test_one_role_may_still_be_held_whole_tenant_only_once(self):
        """Splitting the constraint must not have relaxed what it guaranteed."""
        hq = self.person(self.tenant, "hq-dup@grant.test")
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, hq, role)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_assignment(self.tenant, hq, role)

    def test_one_role_may_still_be_held_at_one_branch_only_once(self):
        hq = self.person(self.tenant, "hq-dupbranch@grant.test")
        role = self.role_granting(self.tenant, "Storekeeper")
        make_assignment(self.tenant, hq, role, branch=self.ikeja)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_assignment(self.tenant, hq, role, branch=self.ikeja)

    def test_whole_tenant_grant_beats_a_branch_grant_held_alongside_it(self):
        """Holding a site role as well must not shrink somebody's existing reach."""
        officer = self.person(self.tenant, "officer@grant.test")
        wide = self.role_granting(self.tenant, "Finance Officer")
        narrow = self.role_granting(self.tenant, "Storekeeper Ikeja")
        make_assignment(self.tenant, officer, wide)
        make_assignment(self.tenant, officer, narrow, branch=self.ikeja)

        self.assertIs(visible_branch_ids(officer, self.tenant), WHOLE_TENANT)

    def test_user_branch_still_narrows_a_whole_tenant_grant_holder(self):
        """Acceptance 4: today's arrangement keeps behaving exactly as it does.

        Access from a whole-tenant grant, visibility from ``User.branch``.
        """
        legacy = self.person(self.tenant, "legacy@grant.test", branch=self.lekki)
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, legacy, role)

        self.assertTrue(has_permission(legacy, BURSAR_KEY, tenant=self.tenant))
        self.assertEqual(visible_branch_ids(legacy, self.tenant), frozenset({self.lekki.pk}))

    def test_user_branch_still_narrows_someone_with_no_grants_at_all(self):
        """Access may come from a personal override, so this must not go empty."""
        legacy = self.person(self.tenant, "legacy-none@grant.test", branch=self.lekki)
        self.assertEqual(visible_branch_ids(legacy, self.tenant), frozenset({self.lekki.pk}))

    def test_branch_grants_outrank_a_stale_user_branch(self):
        """The grants are the record of where somebody works; the column is a default."""
        sunday = self.person(self.tenant, "sunday-stale@grant.test", branch=self.ikeja)
        ikeja_role = self.role_granting(self.tenant, "Storekeeper Ikeja")
        lekki_role = self.role_granting(self.tenant, "Storekeeper Lekki")
        make_assignment(self.tenant, sunday, ikeja_role, branch=self.ikeja)
        make_assignment(self.tenant, sunday, lekki_role, branch=self.lekki)

        self.assertEqual(
            visible_branch_ids(sunday, self.tenant),
            frozenset({self.ikeja.pk, self.lekki.pk}),
        )

    def test_a_branchless_tenant_is_never_narrowed(self):
        """Where a school has no branches the dimension must recede entirely."""
        solo = self.person(self.flat_tenant, "solo@grant.test")
        role = self.role_granting(self.flat_tenant, "Finance Officer")
        make_assignment(self.flat_tenant, solo, role)

        self.assertTrue(has_permission(solo, BURSAR_KEY, tenant=self.flat_tenant))
        self.assertIs(visible_branch_ids(solo, self.flat_tenant), WHOLE_TENANT)

    def test_revoked_branch_grant_stops_counting(self):
        adebayo = self.person(self.tenant, "adebayo-revoked@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        assignment = make_assignment(self.tenant, adebayo, role, branch=self.ikeja)
        assignment.assignment_status = TenantUserRoleAssignment.AssignmentStatus.REVOKED
        assignment.save(update_fields=["assignment_status"])

        self.assertFalse(has_permission(adebayo, BURSAR_KEY, tenant=self.tenant))
        self.assertIs(visible_branch_ids(adebayo, self.tenant), WHOLE_TENANT)


class WithdrawnBranchTests(_BranchGrantFixture):
    """Acceptance 6: losing a branch must never be a way of gaining reach."""

    def _suspend(self, branch):
        branch.suspend(actor_id="test", reason="Closed for the term")

    @staticmethod
    def _next_request(user):
        """The same person as a fresh request would see them.

        Both caches here live on the user *instance*, and DRF rebuilds
        ``request.user`` per request, so re-fetching is what the next request
        actually does. ``refresh_from_db`` would not do it - it reloads columns
        and leaves attributes alone.
        """
        from vs_user.models import User

        return User.objects.get(pk=user.pk)

    def test_suspending_the_only_granted_branch_withdraws_the_permission(self):
        adebayo = self.person(self.tenant, "adebayo-suspend@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)
        self.assertTrue(has_permission(adebayo, BURSAR_KEY, tenant=self.tenant))

        self._suspend(self.ikeja)
        adebayo = self._next_request(adebayo)
        self.assertFalse(has_permission(adebayo, BURSAR_KEY, tenant=self.tenant))

    def test_suspending_the_only_granted_branch_shows_nothing_not_everything(self):
        """The trap: an empty answer must not fall through to "no narrowing"."""
        adebayo = self.person(self.tenant, "adebayo-empty@grant.test")
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)

        self._suspend(self.ikeja)
        adebayo = self._next_request(adebayo)
        visible = visible_branch_ids(adebayo, self.tenant)
        self.assertEqual(visible, frozenset())
        self.assertIsNot(visible, WHOLE_TENANT)

    def test_suspending_the_only_granted_branch_ignores_a_stale_user_branch(self):
        """Withdrawing the site must not hand the caller their old home posting."""
        adebayo = self.person(self.tenant, "adebayo-stale@grant.test", branch=self.lekki)
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)

        self._suspend(self.ikeja)
        adebayo = self._next_request(adebayo)
        self.assertEqual(visible_branch_ids(adebayo, self.tenant), frozenset())

    def test_suspending_one_of_two_branches_leaves_the_other(self):
        sunday = self.person(self.tenant, "sunday-suspend@grant.test")
        ikeja_role = self.role_granting(self.tenant, "Storekeeper Ikeja")
        lekki_role = self.role_granting(self.tenant, "Storekeeper Lekki")
        make_assignment(self.tenant, sunday, ikeja_role, branch=self.ikeja)
        make_assignment(self.tenant, sunday, lekki_role, branch=self.lekki)

        self._suspend(self.ikeja)
        sunday = self._next_request(sunday)
        self.assertEqual(visible_branch_ids(sunday, self.tenant), frozenset({self.lekki.pk}))
        self.assertTrue(has_permission(sunday, BURSAR_KEY, tenant=self.tenant))

    def test_every_out_of_service_state_withdraws_the_grant(self):
        for state in sorted(BranchStatus.values):
            with self.subTest(state=state):
                branch = make_branch(self.tenant, name=f"Site {state}", is_main=False)
                person = self.person(self.tenant, f"{state.lower()}@withdraw.test")
                role = self.role_granting(self.tenant, f"Bursar {state}")
                make_assignment(self.tenant, person, role, branch=branch)

                branch.status = state
                branch.save(update_fields=["status"])
                person = self._next_request(person)

                expected = state not in branch.OUT_OF_SERVICE_STATES
                self.assertEqual(
                    has_permission(person, BURSAR_KEY, tenant=self.tenant), expected,
                )


class BranchScopeQueryCostTests(_BranchGrantFixture):
    """The branch answer is on the hot path, so it is resolved once per request."""

    def test_visible_branches_are_resolved_once_per_user_per_tenant(self):
        sunday = self.person(self.tenant, "sunday-cost@grant.test")
        role = self.role_granting(self.tenant, "Storekeeper")
        make_assignment(self.tenant, sunday, role, branch=self.ikeja)

        with self.assertNumQueries(1):
            first = visible_branch_ids(sunday, self.tenant)
        with self.assertNumQueries(0):
            second = visible_branch_ids(sunday, self.tenant)
        self.assertEqual(first, second)

    def test_a_home_posting_costs_one_query_not_two(self):
        """The legacy fallback must not dereference ``User.branch`` to read its id."""
        legacy = self.person(self.tenant, "legacy-cost@grant.test", branch=self.lekki)
        legacy = type(legacy).objects.get(pk=legacy.pk)  # Unwarmed, as a request is.

        with self.assertNumQueries(1):
            self.assertEqual(
                visible_branch_ids(legacy, self.tenant), frozenset({self.lekki.pk}),
            )
