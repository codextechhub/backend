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
from django.db.models import Q
from django.test import TestCase

from vs_rbac.evaluator import ANY_BRANCH, get_effective_permissions, has_permission
from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.scoping import (
    WHOLE_TENANT,
    branch_scope_for_user,
    visible_branch_ids,
)
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
        self.school = make_school(slug="grant-multi", name="Multi Branch")
        self.tenant = self.school.tenant
        # Yaba carries the main flag, not Ikeja: these tests suspend Ikeja to
        # withdraw a grant, and a school's *main* branch may not leave service
        # at all (Branch._assert_may_leave_service). Which branch is canonical
        # is irrelevant to every assertion below.
        self.ikeja = make_branch(self.tenant, name="Ikeja", is_main=False)
        self.lekki = make_branch(self.tenant, name="Lekki", is_main=False)
        self.yaba = make_branch(self.tenant, name="Yaba", is_main=True)

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
        # No branch is a school-wide posting, and the same STAFF persona.
        return make_staff_user(branch, email=email, tenant=tenant)


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

    def test_a_whole_tenant_grant_outranks_the_holders_home_posting(self):
        """A school-wide role means the school, whatever the staff record says.

        This assertion used to read ``frozenset({self.lekki.pk})``, on the
        grounds that access came from the grant and visibility from
        ``User.branch``. It cannot: two people holding the identical grant then
        saw different schools depending on whether their staff record named a
        site, which makes a home posting into a permission. The grant decides.
        """
        legacy = self.person(self.tenant, "legacy@grant.test", branch=self.lekki)
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, legacy, role)

        self.assertTrue(has_permission(legacy, BURSAR_KEY, tenant=self.tenant))
        self.assertIs(visible_branch_ids(legacy, self.tenant), WHOLE_TENANT)

    def test_the_same_grant_answers_the_same_with_or_without_a_home_posting(self):
        """The defect stated as the thing it broke: two colleagues, one role."""
        role = self.role_granting(self.tenant, "Finance Officer")
        posted = self.person(self.tenant, "posted@grant.test", branch=self.ikeja)
        unposted = self.person(self.tenant, "unposted@grant.test")
        make_assignment(self.tenant, posted, role)
        make_assignment(self.tenant, unposted, role)

        self.assertIs(
            visible_branch_ids(posted, self.tenant),
            visible_branch_ids(unposted, self.tenant),
        )
        self.assertIs(visible_branch_ids(posted, self.tenant), WHOLE_TENANT)

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


class WholeTenantGrantReachTests(_BranchGrantFixture):
    """The four situations side by side, so none can be mistaken for another.

    ``visible_branch_ids`` answers in two *shapes* - ``WHOLE_TENANT`` or a
    frozenset - but four situations stand behind them:

    ===============================  =============================================
    the caller                       what they see
    ===============================  =============================================
    holds a whole-tenant grant       every branch, home posting or not
    holds branch-pinned grants only  exactly those branches
    holds no grants at all           their home posting, or every branch if none
    holds grants at withdrawn sites  nothing at all
    ===============================  =============================================

    The first and the third used to be the same answer inside ``_grant_scope``,
    which is what let a home posting narrow a school-wide grant. Each test below
    names one row, and the last two are asserted against each other as well as
    against themselves: ``frozenset()`` and "the whole tenant" are opposite
    answers a single typo apart, and the typo widens rather than narrows.
    """

    def setUp(self):
        super().setUp()
        # The third shape of school, and the commonest one: exactly one branch.
        # A multi-branch school proves the grant reaches every site; this one
        # proves the answer stays "the whole tenant" rather than becoming
        # "the id of the only branch", which would put a branch filter on every
        # query in a school that has no branch dimension to speak of.
        self.solo_school = make_school(slug="grant-solo", name="One Site School")
        self.solo_tenant = self.solo_school.tenant
        self.solo_branch = make_branch(self.solo_tenant, name="Main", is_main=True)

    def test_a_whole_tenant_holder_with_a_home_posting_sees_every_branch(self):
        """Mrs Adebayo: Finance Officer for the school, staff record says Ikeja."""
        adebayo = self.person(self.tenant, "adebayo-wide@grant.test", branch=self.ikeja)
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, adebayo, role)

        scope = branch_scope_for_user(adebayo, tenant=self.tenant)
        self.assertIs(visible_branch_ids(adebayo, self.tenant), WHOLE_TENANT)
        self.assertFalse(scope.is_narrowed)
        self.assertEqual(scope.q(), Q())

    def test_access_and_visibility_agree_for_a_posted_whole_tenant_holder(self):
        """The invariant this module exists to hold, at the case that broke it.

        ``_assignment_branch_q`` has always matched a whole-tenant grant against
        *any* named branch, so the gate let Mrs Adebayo act on a Lekki document
        while the list filter hid it from her. Two mechanisms, one answer, is the
        whole point of resolving both from the grants.
        """
        adebayo = self.person(self.tenant, "adebayo-agree@grant.test", branch=self.ikeja)
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, adebayo, role)

        for branch in (self.ikeja, self.lekki, self.yaba):
            with self.subTest(branch=branch.name):
                self.assertTrue(
                    has_permission(
                        adebayo, BURSAR_KEY, tenant=self.tenant, branch=branch,
                    ),
                    "the gate admits this branch",
                )
                # The read side's own predicate (see ``caller_may_use_branch``).
                ids = visible_branch_ids(adebayo, self.tenant)
                self.assertTrue(
                    ids is WHOLE_TENANT or branch.pk in ids,
                    "so the rows at that branch must be visible too",
                )

    def test_a_whole_tenant_holder_without_a_home_posting_sees_every_branch(self):
        """Her colleague, whose staff record happens to name no site."""
        colleague = self.person(self.tenant, "colleague-wide@grant.test")
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, colleague, role)

        self.assertIs(visible_branch_ids(colleague, self.tenant), WHOLE_TENANT)

    def test_a_whole_tenant_holder_is_unnarrowed_in_a_single_branch_school(self):
        """One branch: the dimension recedes, it does not become a filter of one."""
        head = self.person(self.solo_tenant, "head@solo-grant.test", branch=self.solo_branch)
        role = self.role_granting(self.solo_tenant, "Finance Officer")
        make_assignment(self.solo_tenant, head, role)

        self.assertIs(visible_branch_ids(head, self.solo_tenant), WHOLE_TENANT)

    def test_a_whole_tenant_grant_wins_over_a_branch_grant_and_a_home_posting(self):
        """Both narrower facts present at once; the widest grant still decides."""
        officer = self.person(self.tenant, "officer-both@grant.test", branch=self.lekki)
        wide = self.role_granting(self.tenant, "Finance Officer")
        narrow = self.role_granting(self.tenant, "Storekeeper Ikeja")
        make_assignment(self.tenant, officer, wide)
        make_assignment(self.tenant, officer, narrow, branch=self.ikeja)

        self.assertIs(visible_branch_ids(officer, self.tenant), WHOLE_TENANT)

    def test_a_branch_pinned_holder_is_untouched_by_the_widening(self):
        """The people the change must not reach, in both shapes of school."""
        pinned = self.person(self.tenant, "pinned@grant.test", branch=self.lekki)
        role = self.role_granting(self.tenant, "Storekeeper Ikeja")
        make_assignment(self.tenant, pinned, role, branch=self.ikeja)
        self.assertEqual(
            visible_branch_ids(pinned, self.tenant), frozenset({self.ikeja.pk}),
        )

        solo_pinned = self.person(self.solo_tenant, "pinned@solo-grant.test")
        solo_role = self.role_granting(self.solo_tenant, "Storekeeper Main")
        make_assignment(self.solo_tenant, solo_pinned, solo_role, branch=self.solo_branch)
        self.assertEqual(
            visible_branch_ids(solo_pinned, self.solo_tenant),
            frozenset({self.solo_branch.pk}),
        )

    def test_no_grants_at_all_still_falls_back_to_the_home_posting(self):
        """The arm ``User.branch`` keeps, and the one it was always for."""
        stranger = self.person(self.tenant, "stranger-home@grant.test", branch=self.yaba)

        self.assertEqual(
            visible_branch_ids(stranger, self.tenant), frozenset({self.yaba.pk}),
        )

    def test_no_grants_and_no_home_posting_is_still_the_whole_tenant(self):
        """The one place the two shapes legitimately meet, and it is not a grant."""
        stranger = self.person(self.tenant, "stranger-nowhere@grant.test")

        self.assertIs(visible_branch_ids(stranger, self.tenant), WHOLE_TENANT)

    def test_every_granted_branch_withdrawn_sees_nothing_and_is_not_confused(self):
        """Sees nothing, and is neither of the two answers standing beside it.

        The failure this guards is the whole reason the "no grants" case needed a
        value of its own: an empty frozenset and ``WHOLE_TENANT`` are opposite
        answers, and folding either into the other hands a caller the whole
        school. The home posting must not rescue her either.
        """
        adebayo = self.person(self.tenant, "adebayo-gone@grant.test", branch=self.yaba)
        role = self.role_granting(self.tenant, "Bursar")
        make_assignment(self.tenant, adebayo, role, branch=self.ikeja)
        self.ikeja.suspend(actor_id="test", reason="Closed for the term")

        from vs_user.models import User

        adebayo = User.objects.get(pk=adebayo.pk)  # As the next request sees her.
        visible = visible_branch_ids(adebayo, self.tenant)
        self.assertEqual(visible, frozenset())
        self.assertIsNot(visible, WHOLE_TENANT)
        self.assertNotEqual(visible, frozenset({self.yaba.pk}))

    def test_the_widened_answer_memoises_like_any_other(self):
        """One query, then none, and the cached value is the widened one."""
        adebayo = self.person(self.tenant, "adebayo-cache@grant.test", branch=self.ikeja)
        role = self.role_granting(self.tenant, "Finance Officer")
        make_assignment(self.tenant, adebayo, role)

        from vs_user.models import User

        adebayo = User.objects.get(pk=adebayo.pk)  # Unwarmed, as a request is.
        with self.assertNumQueries(1):
            first = visible_branch_ids(adebayo, self.tenant)
        with self.assertNumQueries(0):
            second = visible_branch_ids(adebayo, self.tenant)

        self.assertIs(first, WHOLE_TENANT)
        self.assertIs(second, WHOLE_TENANT)
        # The sentinel is module-private and must never reach the cache.
        self.assertEqual(adebayo._rbac_visible_branches, {self.tenant.pk: WHOLE_TENANT})


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
