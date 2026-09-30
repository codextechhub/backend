"""Turning "which branches?" into "whose rows?" - and what a NULL branch means.

:mod:`vs_rbac.scoping` answers *which branches a caller may work in*, and these
tests pin the half that renders the answer against rows: :class:`BranchScope`,
the ``branch_q`` / ``branch_visible`` renderings of it, and the transaction
helpers. A NULL branch means two different things, and both are asserted here
against real rows rather than against the shape of a ``Q``::

    a shared record (a customer, a vendor, a fee structure) with no branch
    belongs to every branch, and stays visible to a branch-pinned caller;

    a transaction with no branch has not been given its branch yet, and is
    seen only by a caller whose reach is the whole school.

The write half is here too: a transaction is raised for a real branch, and a
chain carries one branch from end to end.
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
    transaction_branch_visible,
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


class TransactionScopeTests(_RowFixture):
    """A transaction with no branch is seen only by a whole-school caller.

    Mrs Adeyemi works at Ikeja only. A refund nobody has given a branch could be
    Lekki's as easily as Ikeja's, so it stays out of their lists; Mr Bello, the
    school-wide bursar, sees it and can give it its branch.
    """

    def setUp(self):
        super().setUp()
        self.at_ikeja = self.row_at(self.tenant, self.ikeja, "x-ikeja@scope.test")
        self.at_lekki = self.row_at(self.tenant, self.lekki, "x-lekki@scope.test")
        self.unbranched = self.row_at(self.tenant, None, "x-shared@scope.test")

    def seen(self, user, tenant=None):
        qs = TenantUserRoleAssignment.objects.filter(
            tenant=tenant or self.tenant, id__in=self.rows,
        )
        return set(
            transaction_branch_visible(self.request_for(user), qs)
            .values_list("id", flat=True)
        )

    def test_a_branch_bound_caller_sees_only_their_own_branch(self):
        adeyemi = self.pinned_at(self.tenant, "x-adeyemi@scope.test", self.ikeja)

        seen = self.seen(adeyemi)

        self.assertEqual(seen, {self.at_ikeja.id})
        self.assertNotIn(self.unbranched.id, seen)
        self.assertNotIn(self.at_lekki.id, seen)

    def test_it_is_the_exclusive_form_of_the_shared_scope(self):
        adeyemi = self.pinned_at(self.tenant, "x-adeyemi2@scope.test", self.ikeja)

        self.assertEqual(self.seen(adeyemi), self.visible(adeyemi, include_shared=False))

    def test_a_whole_school_caller_still_sees_the_unbranched_rows(self):
        bello = self.whole_tenant(self.tenant, "x-bello@scope.test")

        self.assertEqual(
            self.seen(bello), {self.at_ikeja.id, self.at_lekki.id, self.unbranched.id},
        )

    def test_a_one_branch_school_is_unchanged(self):
        """Harbour's bursar pinned to Main reaches the whole school, unbranched rows too."""
        at_main = self.row_at(self.solo_tenant, self.solo_main, "x-solo-a@scope.test")
        unbranched = self.row_at(self.solo_tenant, None, "x-solo-b@scope.test")
        caretaker = self.pinned_at(self.solo_tenant, "x-solo@scope.test", self.solo_main)

        seen = self.seen(caretaker, self.solo_tenant)

        self.assertIn(at_main.id, seen)
        self.assertIn(unbranched.id, seen)


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

        The rival opens a second branch first, so the outsider's pin narrows
        them. With one branch they would reach their whole tenant, and then the
        predicate would not narrow them at all, exactly as it does not narrow a
        rival holding a whole-tenant grant.
        """
        make_branch(self.rival_tenant, name="Lekki", is_main=False)
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

    def test_the_one_branch_rule_only_widens_a_caller_at_home(self):
        """Pinned to their own tenant's only branch: whole-tenant there, and nowhere else."""
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

        outsider = make_staff_user(self.rival_ikeja, email="home-only@scope.test")
        make_assignment(
            self.rival_tenant, outsider,
            self.role_granting(self.rival_tenant, "Bursar home-only"),
            branch=self.rival_ikeja,
        )

        self.assertIs(visible_branch_ids(outsider, self.rival_tenant), WHOLE_TENANT)
        self.assertEqual(
            visible_branch_ids(outsider, self.solo_tenant),
            frozenset({self.rival_ikeja.pk}),
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


# --------------------------------------------------------------------------- #
# The write half                                                              #
# --------------------------------------------------------------------------- #


class SoleCallerBranchTests(_RowFixture):
    """"Exactly one branch, or no answer" - and why it is never used to gate.

    :func:`sole_caller_branch_id` answers ``None`` in two completely different
    situations: the caller is unbound, and the caller is bound to several. That
    ambiguity is safe only because the answer is used as a *default*, never as a
    permission. These tests pin that it stays a default-shaped answer.
    """

    def test_one_branch_answers_that_branch(self):
        from vs_rbac.scoping import sole_caller_branch_id

        user = self.pinned_at(self.tenant, "sole-one@t.com", self.ikeja)

        self.assertEqual(
            sole_caller_branch_id(self.request_for(user)), self.ikeja.pk,
        )

    def test_two_branches_answer_nothing_rather_than_picking(self):
        from vs_rbac.scoping import sole_caller_branch_id

        user = self.pinned_at(self.tenant, "sole-two@t.com", self.ikeja, self.lekki)

        self.assertIsNone(sole_caller_branch_id(self.request_for(user)))

    def test_an_unbound_caller_answers_nothing_too(self):
        """Deliberately the same answer as "two branches", and deliberately unusable
        as a permission because of it."""
        from vs_rbac.scoping import sole_caller_branch_id

        user = self.whole_tenant(self.tenant, "sole-hq@t.com")

        self.assertIsNone(sole_caller_branch_id(self.request_for(user)))

    def test_a_single_branch_school_answers_its_only_branch(self):
        from vs_rbac.scoping import sole_caller_branch_id

        user = self.pinned_at(self.solo_tenant, "sole-solo@t.com", self.solo_main)

        self.assertEqual(
            sole_caller_branch_id(self.request_for(user)), self.solo_main.pk,
        )


class RaisedBranchRuleTests(_RowFixture):
    """The rule that decides what branch goes on a row somebody creates.

    Asserted here at the unit boundary and again over HTTP in
    ``vs_finance.tests_branch_write`` / ``vs_procurement.tests``. This layer is
    where the *shape* of the rule is pinned - in particular that the two readings
    of the ambiguous case differ in exactly one place and nowhere else.
    """

    def raised(self, user, body=None, **kwargs):
        from vs_rbac.scoping import raised_branch

        return raised_branch(
            self.request_for(user), self.tenant, body or {}, **kwargs,
        )

    def test_a_pinned_caller_gets_their_own_branch_without_naming_it(self):
        user = self.pinned_at(self.tenant, "raise-one@t.com", self.ikeja)

        self.assertEqual(self.raised(user), self.ikeja)

    def test_a_pinned_caller_naming_another_branch_is_refused(self):
        from rest_framework.exceptions import PermissionDenied

        user = self.pinned_at(self.tenant, "raise-wrong@t.com", self.ikeja)

        with self.assertRaises(PermissionDenied):
            self.raised(user, {"branch": self.lekki.pk})

    def test_an_unbound_caller_files_a_shared_record_for_every_branch(self):
        user = self.whole_tenant(self.tenant, "raise-hq@t.com")

        self.assertIsNone(self.raised(user))

    def test_an_unbound_caller_may_name_any_branch_in_the_tenant(self):
        user = self.whole_tenant(self.tenant, "raise-hq2@t.com")

        self.assertEqual(self.raised(user, {"branch": self.yaba.pk}), self.yaba)

    def test_the_ambiguous_case_is_asked_by_default(self):
        from rest_framework.exceptions import ValidationError

        user = self.pinned_at(self.tenant, "raise-two@t.com", self.ikeja, self.lekki)

        with self.assertRaises(ValidationError) as caught:
            self.raised(user)
        self.assertIn("branch", caught.exception.detail)

    def test_the_ambiguous_case_is_shared_when_the_call_site_says_so(self):
        """The one axis the two readings differ on.

        Same caller, same empty body, opposite answers - and the only thing that
        changed is a flag the call site had to spell out. Anything else differing
        between the two readings would be a second rule.
        """
        user = self.pinned_at(self.tenant, "raise-two2@t.com", self.ikeja, self.lekki)

        self.assertIsNone(self.raised(user, shared_when_ambiguous=True))

    def test_the_flag_changes_nothing_for_a_caller_who_is_not_ambiguous(self):
        pinned = self.pinned_at(self.tenant, "raise-flag1@t.com", self.ikeja)
        unbound = self.whole_tenant(self.tenant, "raise-flag2@t.com")

        self.assertEqual(
            self.raised(pinned), self.raised(pinned, shared_when_ambiguous=True),
        )
        self.assertEqual(
            self.raised(unbound), self.raised(unbound, shared_when_ambiguous=True),
        )

    def test_a_caller_whose_every_branch_was_withdrawn_may_not_create(self):
        """Withdrawing a site withdraws what it carried, on the write side too."""
        from rest_framework.exceptions import PermissionDenied

        from vs_tenants.models import BranchStatus

        user = self.pinned_at(self.tenant, "raise-gone@t.com", self.ikeja)
        self.ikeja.status = BranchStatus.SUSPENDED
        self.ikeja.save(update_fields=["status"])

        with self.assertRaises(PermissionDenied):
            self.raised(user)

    def test_another_tenants_branch_is_reported_like_an_unknown_one(self):
        """No id oracle: a real foreign branch and a fictional id are one answer."""
        from rest_framework.exceptions import ValidationError

        user = self.whole_tenant(self.tenant, "raise-oracle@t.com")

        with self.assertRaises(ValidationError) as foreign:
            self.raised(user, {"branch": self.rival_ikeja.pk})
        with self.assertRaises(ValidationError) as unknown:
            self.raised(user, {"branch": 99_999_999})

        self.assertEqual(foreign.exception.detail, unknown.exception.detail)


class RaisedTransactionBranchTests(_RowFixture):
    """A transaction is always raised for a real branch."""

    def raised(self, user, body=None, tenant=None):
        from vs_rbac.scoping import raised_transaction_branch

        return raised_transaction_branch(
            self.request_for(user), tenant or self.tenant, body or {},
        )

    def test_a_pinned_caller_gets_their_own_branch_without_naming_it(self):
        adeyemi = self.pinned_at(self.tenant, "rt-one@t.com", self.ikeja)

        self.assertEqual(self.raised(adeyemi), self.ikeja)

    def test_a_pinned_caller_naming_another_branch_is_refused(self):
        from rest_framework.exceptions import PermissionDenied

        adeyemi = self.pinned_at(self.tenant, "rt-wrong@t.com", self.ikeja)

        with self.assertRaises(PermissionDenied):
            self.raised(adeyemi, {"branch": self.lekki.pk})

    def test_a_caller_in_two_branches_names_one(self):
        from rest_framework.exceptions import ValidationError

        okafor = self.pinned_at(self.tenant, "rt-two@t.com", self.ikeja, self.lekki)

        with self.assertRaises(ValidationError) as caught:
            self.raised(okafor)
        self.assertIn("branch", caught.exception.detail)
        self.assertEqual(self.raised(okafor, {"branch": self.lekki.pk}), self.lekki)

    def test_a_whole_school_caller_at_a_school_with_several_branches_names_one(self):
        """No school-wide transaction: Mr Bello's unnamed refund is a 400, not NULL."""
        from rest_framework.exceptions import ValidationError

        bello = self.whole_tenant(self.tenant, "rt-hq@t.com")

        with self.assertRaises(ValidationError) as caught:
            self.raised(bello)
        self.assertIn("branch", caught.exception.detail)
        self.assertEqual(self.raised(bello, {"branch": self.yaba.pk}), self.yaba)

    def test_a_one_branch_school_gets_its_only_branch_without_asking(self):
        unpinned = self.whole_tenant(self.solo_tenant, "rt-solo-hq@t.com")
        pinned = self.pinned_at(self.solo_tenant, "rt-solo-pin@t.com", self.solo_main)

        self.assertEqual(self.raised(unpinned, tenant=self.solo_tenant), self.solo_main)
        self.assertEqual(self.raised(pinned, tenant=self.solo_tenant), self.solo_main)

    def test_another_tenants_branch_is_reported_like_an_unknown_one(self):
        from rest_framework.exceptions import ValidationError

        bello = self.whole_tenant(self.tenant, "rt-oracle@t.com")

        with self.assertRaises(ValidationError) as foreign:
            self.raised(bello, {"branch": self.rival_ikeja.pk})
        with self.assertRaises(ValidationError) as unknown:
            self.raised(bello, {"branch": 99_999_999})

        self.assertEqual(foreign.exception.detail, unknown.exception.detail)


class InheritedBranchRuleTests(_RowFixture):
    """The rule that decides what branch a row copies from the row it continues."""

    def inherited(self, user, *sources):
        from vs_rbac.scoping import inherited_branch_id

        return inherited_branch_id(self.request_for(user), *sources)

    def source_at(self, branch):
        return SimpleNamespace(branch_id=getattr(branch, "pk", None))

    def test_the_source_decides_for_an_unbound_caller(self):
        user = self.whole_tenant(self.tenant, "inh-hq@t.com")

        self.assertEqual(
            self.inherited(user, self.source_at(self.lekki)), self.lekki.pk,
        )

    def test_a_pinned_caller_may_continue_their_own_chain(self):
        user = self.pinned_at(self.tenant, "inh-own@t.com", self.ikeja)

        self.assertEqual(
            self.inherited(user, self.source_at(self.ikeja)), self.ikeja.pk,
        )

    def test_a_pinned_caller_may_not_continue_another_branchs_chain(self):
        from rest_framework.exceptions import PermissionDenied

        user = self.pinned_at(self.tenant, "inh-other@t.com", self.ikeja)

        with self.assertRaises(PermissionDenied):
            self.inherited(user, self.source_at(self.lekki))

    def test_an_unbranched_source_is_continued_only_by_a_whole_school_caller(self):
        """They cannot read it, so they cannot build on it; Mr Bello can, and it
        stays unbranched.
        """
        from rest_framework.exceptions import PermissionDenied

        adeyemi = self.pinned_at(self.tenant, "inh-shared@t.com", self.ikeja)
        bello = self.whole_tenant(self.tenant, "inh-shared-hq@t.com")

        with self.assertRaises(PermissionDenied):
            self.inherited(adeyemi, self.source_at(None))
        self.assertIsNone(self.inherited(bello, self.source_at(None)))

    def test_sources_from_two_branches_are_refused(self):
        """A payment settling an Ikeja bill and a Lekki bill has no branch to be booked to."""
        from rest_framework.exceptions import ValidationError

        bello = self.whole_tenant(self.tenant, "inh-split@t.com")

        with self.assertRaises(ValidationError) as caught:
            self.inherited(bello, self.source_at(self.ikeja), self.source_at(self.lekki))
        self.assertIn("branch", caught.exception.detail)

    def test_an_unbranched_source_beside_a_branched_one_is_refused_at_a_school_with_several(self):
        from rest_framework.exceptions import ValidationError

        bello = self.whole_tenant(self.tenant, "inh-mixed@t.com")

        with self.assertRaises(ValidationError):
            self.inherited(bello, self.source_at(self.ikeja), self.source_at(None))

    def test_a_one_branch_school_continues_an_unbranched_source_into_its_branch(self):
        harbour = self.whole_tenant(self.solo_tenant, "inh-solo@t.com")

        self.assertEqual(
            self.inherited(harbour, self.source_at(None), self.source_at(self.solo_main)),
            self.solo_main.pk,
        )

    def test_a_missing_source_is_skipped_rather_than_counted_as_a_disagreement(self):
        user = self.whole_tenant(self.tenant, "inh-none@t.com")

        self.assertEqual(
            self.inherited(user, None, self.source_at(self.yaba)), self.yaba.pk,
        )


class SameTransactionBranchTests(_RowFixture):
    """Which transactions belong to one branch, for settlement and the bank rule."""

    def test_equal_branches_match_and_different_ones_do_not(self):
        from vs_rbac.scoping import same_transaction_branch

        self.assertTrue(same_transaction_branch(self.tenant, self.ikeja.pk, self.ikeja))
        self.assertFalse(same_transaction_branch(self.tenant, self.ikeja, self.lekki))

    def test_an_unbranched_row_matches_only_another_at_a_school_with_several(self):
        from vs_rbac.scoping import same_transaction_branch

        self.assertFalse(same_transaction_branch(self.tenant, self.ikeja, None))
        self.assertTrue(same_transaction_branch(self.tenant, None, None))

    def test_an_unbranched_row_is_the_only_branchs_at_a_one_branch_school(self):
        from vs_rbac.scoping import same_transaction_branch

        self.assertTrue(same_transaction_branch(self.solo_tenant, self.solo_main, None))
        self.assertTrue(same_transaction_branch(self.solo_tenant.pk, None, self.solo_main.pk))

    def test_the_q_selects_the_same_rows_the_predicate_accepts(self):
        from vs_rbac.scoping import transaction_branch_match_q

        at_main = self.row_at(self.solo_tenant, self.solo_main, "m-a@t.com")
        unbranched_solo = self.row_at(self.solo_tenant, None, "m-b@t.com")
        at_ikeja = self.row_at(self.tenant, self.ikeja, "m-c@t.com")
        unbranched = self.row_at(self.tenant, None, "m-d@t.com")
        rows = TenantUserRoleAssignment.objects.filter(id__in=self.rows)

        def ids(tenant, branch):
            return set(rows.filter(tenant=tenant).filter(
                transaction_branch_match_q(tenant, branch)).values_list("id", flat=True))

        self.assertEqual(ids(self.solo_tenant, self.solo_main), {at_main.id, unbranched_solo.id})
        self.assertEqual(ids(self.solo_tenant, None), {at_main.id, unbranched_solo.id})
        self.assertEqual(ids(self.tenant, self.ikeja), {at_ikeja.id})
        self.assertEqual(ids(self.tenant, None), {unbranched.id})
