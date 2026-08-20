"""Turning "which branches?" into "whose rows?" - and the null-branch trap.

:mod:`vs_rbac.scoping` has answered *which branches a caller may work in* since
branch-scoped grants started working. Nothing outside :mod:`vs_procurement` ever
asked. The gate held - a branch-pinned grant opened the screen - and the
narrowing never happened, so a "Bursar at Ikeja" opened the fee screens and read
Lekki's and Yaba's rows too.

These tests pin the half that closes that: :class:`BranchScope` and the
``branch_q`` / ``branch_visible`` renderings of it. The rule they exist to
protect is the inclusive one::

    a row whose branch is NULL is shared across the school,
    and stays visible to a branch-pinned caller

Getting that backwards is the quiet failure. A branch admin who silently loses
every school-wide fee structure sees missing data, not a permission error, and
missing data gets reported as a broken screen or not reported at all. So the
inclusive reading is the default, the exclusive one has to be asked for by name
(:mod:`vs_procurement` asks, deliberately), and both are asserted here against
real rows rather than against the shape of a ``Q``.
"""
from types import SimpleNamespace

from django.test import TestCase

from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.scoping import (
    UNNARROWED,
    WHOLE_TENANT,
    BranchScope,
    branch_q,
    branch_scope,
    branch_visible,
    caller_branch_ids,
)

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_staff_user,
)

BURSAR_KEY = "finance.invoice.view"


class _RowFixture(TestCase):
    """A multi-branch school, a single-branch school, and a rival.

    ``TenantUserRoleAssignment`` is the model under filter, chosen because
    :mod:`vs_rbac` owns it and its ``branch`` is nullable in exactly the way every
    other branch-bearing model's is - so what these tests prove about the
    predicate transfers to an invoice, a ticket or a workflow instance without
    those apps having to be installed to prove it.

    Three branches, not two: the third is what catches a predicate that quietly
    means "any branch" rather than "the ones I hold".
    """

    def setUp(self):
        self.school = make_school(slug="scope-multi", name="Multi Branch")
        self.tenant = self.school.tenant
        self.ikeja = make_branch(self.tenant, name="Ikeja", is_main=False)
        self.lekki = make_branch(self.tenant, name="Lekki", is_main=False)
        self.yaba = make_branch(self.tenant, name="Yaba", is_main=True)

        # The other shape of school: one branch, where the dimension recedes.
        self.solo_school = make_school(slug="scope-solo", name="Single Branch")
        self.solo_tenant = self.solo_school.tenant
        self.solo_main = make_branch(self.solo_tenant, name="Main", is_main=True)

        self.rival_school = make_school(slug="scope-rival", name="Rival Group")
        self.rival_tenant = self.rival_school.tenant
        self.rival_ikeja = make_branch(self.rival_tenant, name="Ikeja", is_main=True)

        self.permission = make_permission(BURSAR_KEY)
        #: Only the rows :meth:`row_at` made. Every caller below is *also* a row in
        #: this model (their grant), and those would otherwise drift into the
        #: answers and make the assertions depend on who else the fixture built.
        self.rows = []

    # -- people --------------------------------------------------------------- #

    def role_granting(self, tenant, name):
        role = make_role(tenant, name=name)
        make_role_permission(role, self.permission, granted=True)
        return role

    def pinned_at(self, tenant, email, *branches):
        """Somebody whose only access is a grant pinned to each of *branches*."""
        user = make_staff_user(None, email=email, tenant=tenant)
        for i, branch in enumerate(branches):
            role = self.role_granting(tenant, f"Bursar {email} {i}")
            make_assignment(tenant, user, role, branch=branch)
        return user

    def whole_tenant(self, tenant, email):
        """Somebody holding the same key across the whole school."""
        user = make_staff_user(None, email=email, tenant=tenant)
        make_assignment(tenant, user, self.role_granting(tenant, f"HQ {email}"))
        return user

    def request_for(self, user):
        """The bit of a DRF request the scoping helpers actually read."""
        return SimpleNamespace(user=user)

    # -- rows ----------------------------------------------------------------- #

    def row_at(self, tenant, branch, email):
        """One assignment row belonging to ``branch`` (or to the school, when None)."""
        holder = make_staff_user(None, email=email, tenant=tenant)
        role = self.role_granting(tenant, f"Row {email}")
        row = make_assignment(tenant, holder, role, branch=branch)
        self.rows.append(row.id)
        return row

    def visible(self, user, **kwargs):
        """The row ids ``user`` may see, through the shared renderer."""
        qs = TenantUserRoleAssignment.objects.filter(
            tenant=self.tenant, id__in=self.rows,
        )
        return set(
            branch_visible(self.request_for(user), qs, **kwargs)
            .values_list("id", flat=True)
        )


class InclusiveByDefaultTests(_RowFixture):
    """A null branch is shared across the school, and stays visible."""

    def setUp(self):
        super().setUp()
        self.at_ikeja = self.row_at(self.tenant, self.ikeja, "row-ikeja@scope.test")
        self.at_lekki = self.row_at(self.tenant, self.lekki, "row-lekki@scope.test")
        self.at_yaba = self.row_at(self.tenant, self.yaba, "row-yaba@scope.test")
        self.school_wide = self.row_at(self.tenant, None, "row-shared@scope.test")

    def test_a_branch_pinned_caller_sees_their_branch_and_the_shared_rows(self):
        """The headline rule, and the one most likely to be got backwards.

        The shared row is asserted *by name* rather than as part of a set, because
        an exclusive predicate still passes "sees Ikeja, not Lekki" and fails only
        here.
        """
        adebayo = self.pinned_at(self.tenant, "adebayo@scope.test", self.ikeja)

        seen = self.visible(adebayo)

        self.assertIn(self.school_wide.id, seen)
        self.assertIn(self.at_ikeja.id, seen)
        self.assertNotIn(self.at_lekki.id, seen)
        self.assertNotIn(self.at_yaba.id, seen)

    def test_a_caller_pinned_to_two_branches_sees_both_and_not_the_third(self):
        """A set, not a branch: ``User.branch`` could never express this."""
        sunday = self.pinned_at(
            self.tenant, "sunday@scope.test", self.ikeja, self.lekki,
        )

        seen = self.visible(sunday)

        self.assertEqual(
            seen, {self.at_ikeja.id, self.at_lekki.id, self.school_wide.id},
        )
        self.assertNotIn(self.at_yaba.id, seen)

    def test_a_whole_tenant_caller_sees_everything_exactly_as_before(self):
        """The common case since a null branch became normal for school users."""
        hq = self.whole_tenant(self.tenant, "hq@scope.test")

        self.assertIs(caller_branch_ids(self.request_for(hq)), WHOLE_TENANT)
        self.assertEqual(
            self.visible(hq),
            {self.at_ikeja.id, self.at_lekki.id, self.at_yaba.id,
             self.school_wide.id},
        )

    def test_a_whole_tenant_caller_gets_byte_identical_sql(self):
        """Not merely "sees everything" - the query must not change at all.

        A narrowing that adds a tautological ``OR branch IS NULL`` to every read on
        the platform would be a performance change disguised as a security fix, so
        the unbound path returns the queryset untouched rather than filtered.
        """
        hq = self.whole_tenant(self.tenant, "hq-sql@scope.test")
        qs = TenantUserRoleAssignment.objects.filter(tenant=self.tenant)

        narrowed = branch_visible(self.request_for(hq), qs)

        self.assertIs(narrowed, qs)
        self.assertEqual(str(narrowed.query), str(qs.query))

    def test_a_single_branch_school_is_not_narrowed_by_a_pinned_grant(self):
        """One branch: the dimension recedes, and every row is still reachable.

        A school with one branch is the common shape, and a grant pinned to its
        only branch must not start hiding the school-wide rows that branch shares
        the school with.
        """
        solo_at_main = self.row_at(self.solo_tenant, self.solo_main, "solo-a@scope.test")
        solo_shared = self.row_at(self.solo_tenant, None, "solo-b@scope.test")
        caretaker = self.pinned_at(
            self.solo_tenant, "caretaker@scope.test", self.solo_main,
        )

        qs = TenantUserRoleAssignment.objects.filter(
            tenant=self.solo_tenant, id__in=self.rows,
        )
        seen = set(
            branch_visible(self.request_for(caretaker), qs)
            .values_list("id", flat=True)
        )

        self.assertIn(solo_at_main.id, seen)
        self.assertIn(solo_shared.id, seen)


class ExclusiveOnRequestTests(_RowFixture):
    """The opt-in reading, where a null branch is a scope of its own."""

    def setUp(self):
        super().setUp()
        self.at_ikeja = self.row_at(self.tenant, self.ikeja, "x-ikeja@scope.test")
        self.school_wide = self.row_at(self.tenant, None, "x-shared@scope.test")

    def test_the_exclusive_form_withholds_the_school_wide_rows(self):
        """What :mod:`vs_procurement` asks for, and why it has to be asked for."""
        adebayo = self.pinned_at(self.tenant, "x-adebayo@scope.test", self.ikeja)

        seen = self.visible(adebayo, include_shared=False)

        self.assertIn(self.at_ikeja.id, seen)
        self.assertNotIn(self.school_wide.id, seen)

    def test_the_exclusive_form_still_does_not_narrow_a_whole_tenant_caller(self):
        """Exclusive is about the null rows, not about widening or narrowing reach."""
        hq = self.whole_tenant(self.tenant, "x-hq@scope.test")

        seen = self.visible(hq, include_shared=False)

        self.assertIn(self.at_ikeja.id, seen)
        self.assertIn(self.school_wide.id, seen)


class WithdrawnBranchTests(_RowFixture):
    """Every granted branch withdrawn means nothing, not everything."""

    def test_an_empty_grant_set_shows_only_the_school_wide_rows(self):
        """An empty frozenset is a real answer and must not read as "unbound".

        Inclusive, so what survives is exactly the school-wide rows: the person
        has no branch left to stand in, but has not been ejected from the school.
        """
        from vs_tenants.models import BranchStatus

        at_ikeja = self.row_at(self.tenant, self.ikeja, "w-ikeja@scope.test")
        school_wide = self.row_at(self.tenant, None, "w-shared@scope.test")
        adebayo = self.pinned_at(self.tenant, "w-adebayo@scope.test", self.ikeja)

        self.ikeja.status = BranchStatus.SUSPENDED
        self.ikeja.save(update_fields=["status"])
        seen = self.visible(adebayo)

        self.assertEqual(seen, {school_wide.id})
        self.assertNotIn(at_ikeja.id, seen)

    def test_an_empty_grant_set_is_exclusive_of_everything_when_shared_is_off(self):
        from vs_tenants.models import BranchStatus

        self.row_at(self.tenant, self.ikeja, "w2-ikeja@scope.test")
        self.row_at(self.tenant, None, "w2-shared@scope.test")
        adebayo = self.pinned_at(self.tenant, "w2-adebayo@scope.test", self.ikeja)

        self.ikeja.status = BranchStatus.SUSPENDED
        self.ikeja.save(update_fields=["status"])

        self.assertEqual(self.visible(adebayo, include_shared=False), set())


class TenantBoundaryTests(_RowFixture):
    """The narrowing never widens, and never crosses a tenant.

    Branch narrowing sits *inside* tenant/entity scoping and is not a substitute
    for it. These assert it does not weaken what is already there.
    """

    def test_narrowing_cannot_reach_another_tenants_rows(self):
        rival_row = self.row_at(self.rival_tenant, self.rival_ikeja, "r@scope.test")
        mine = self.row_at(self.tenant, self.ikeja, "m@scope.test")
        adebayo = self.pinned_at(self.tenant, "b-adebayo@scope.test", self.ikeja)

        seen = self.visible(adebayo)

        self.assertIn(mine.id, seen)
        self.assertNotIn(rival_row.id, seen)

    def test_the_branch_predicate_is_not_itself_a_tenant_boundary(self):
        """Stated out loud, because assuming otherwise is how one gets removed.

        A rival's grants resolve against the rival's own tenant, so they say
        nothing about this school's branch ids and cannot match this school's
        branch-bearing rows. But the inclusive arm matches *any* null branch,
        including this school's school-wide rows - so if the surrounding tenant
        filter were ever dropped, an outsider would see them.

        That is not a defect in the predicate; branch narrows *within* a tenant
        and was never the thing keeping tenants apart. It is pinned here so nobody
        reads "branch scoping is on" as "tenant scoping is redundant".
        """
        mine = self.row_at(self.tenant, self.ikeja, "b2-mine@scope.test")
        shared = self.row_at(self.tenant, None, "b2-shared@scope.test")
        outsider = self.pinned_at(
            self.rival_tenant, "outsider@scope.test", self.rival_ikeja,
        )

        # With tenant scoping in place - how every real endpoint reads - nothing.
        self.assertEqual(self.visible(outsider), {shared.id})
        self.assertNotIn(mine.id, self.visible(outsider))

        # Without it, the branch predicate alone does not save you.
        unscoped = TenantUserRoleAssignment.objects.filter(id__in=self.rows)
        leaked = set(
            branch_visible(self.request_for(outsider), unscoped)
            .values_list("id", flat=True)
        )
        self.assertIn(shared.id, leaked)

    def test_the_narrowing_can_only_ever_remove_rows(self):
        """The property that holds for every caller, however their grants are shaped.

        Written as set arithmetic because it is the one assertion that fails for
        *any* way of widening, including ones nobody has thought of yet.
        """
        self.row_at(self.tenant, self.ikeja, "w-a@scope.test")
        self.row_at(self.tenant, self.lekki, "w-b@scope.test")
        self.row_at(self.tenant, None, "w-c@scope.test")
        everything = set(self.rows)

        for label, user in (
            ("pinned to one", self.pinned_at(self.tenant, "p1@scope.test", self.ikeja)),
            ("pinned to two", self.pinned_at(
                self.tenant, "p2@scope.test", self.ikeja, self.lekki)),
            ("whole tenant", self.whole_tenant(self.tenant, "p3@scope.test")),
        ):
            with self.subTest(caller=label):
                self.assertLessEqual(self.visible(user), everything)
                self.assertLessEqual(
                    self.visible(user, include_shared=False), everything,
                )


class RenderingTests(_RowFixture):
    """``BranchScope`` renders one resolved answer against several paths."""

    def test_an_unbound_scope_renders_to_an_empty_q(self):
        from django.db.models import Q

        self.assertEqual(BranchScope(WHOLE_TENANT).q(), Q())
        self.assertFalse(BranchScope(WHOLE_TENANT).is_narrowed)

    def test_a_whole_tenant_caller_resolves_to_the_shared_unnarrowed_scope(self):
        hq = self.whole_tenant(self.tenant, "u-hq@scope.test")
        self.assertIs(branch_scope(self.request_for(hq)), UNNARROWED)

    def test_a_prefix_reaches_the_branch_through_a_relation(self):
        """A report aggregating several models reaches ``branch`` by several routes."""
        scope = BranchScope(frozenset({7}))
        rendered = str(scope.q("role__"))

        self.assertIn("role__branch_id__in", rendered)
        self.assertIn("role__branch_id__isnull", rendered)

    def test_is_narrowed_is_the_only_signal_a_payload_should_read(self):
        """It turns branch columns on; an unbound caller's payload is unchanged."""
        hq = self.whole_tenant(self.tenant, "n-hq@scope.test")
        pinned = self.pinned_at(self.tenant, "n-pinned@scope.test", self.ikeja)

        self.assertFalse(branch_scope(self.request_for(hq)).is_narrowed)
        self.assertTrue(branch_scope(self.request_for(pinned)).is_narrowed)

    def test_branch_q_and_branch_visible_are_the_same_rule(self):
        """Two spellings, one predicate - so a call site may use either."""
        pinned = self.pinned_at(self.tenant, "q-pinned@scope.test", self.ikeja)
        request = self.request_for(pinned)
        qs = TenantUserRoleAssignment.objects.filter(tenant=self.tenant)

        self.assertEqual(
            str(qs.filter(branch_q(request)).query),
            str(branch_visible(request, qs).query),
        )


class AnonymousCallerTests(_RowFixture):
    """An unauthenticated request is unbound, not empty.

    Authentication is a separate gate that has already run by the time anything
    here is asked; answering "sees nothing" would turn a missing gate into silent
    empty pages instead of a 401.
    """

    def test_an_anonymous_request_is_not_narrowed(self):
        from django.contrib.auth.models import AnonymousUser

        request = SimpleNamespace(user=AnonymousUser())
        self.assertIs(caller_branch_ids(request), WHOLE_TENANT)

    def test_a_request_with_no_user_at_all_is_not_narrowed(self):
        self.assertIs(caller_branch_ids(SimpleNamespace()), WHOLE_TENANT)
