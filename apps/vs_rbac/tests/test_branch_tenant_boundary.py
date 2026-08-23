"""The branch-to-tenant boundary, asserted at the level it is written.

Phase C stopped code travelling ``branch.school.tenant`` now that ``Branch``
carries its own ``tenant``. Every rewritten site in ``vs_rbac`` is a guard, and
a guard rewritten wrongly does not raise - it simply stops refusing. These tests
exist so that failure is loud:

* ``TenantRoleTemplate.clean`` and ``TenantUserRoleAssignment.clean`` (the model
  guards, reachable by any code path including services and shells);
* ``TenantScopedRelatedField``'s ``tenant_lookup`` on both branch fields (the
  queryset that makes a foreign branch simply not exist);
* the ``validate_branch`` / ``validate`` backstops, which the API cannot reach
  because the field resolves first, and which are therefore only ever tested
  directly;
* the invariant that no ``TenantAwareManager`` model resolves its tenant
  through ``school`` or ``branch`` any more, which is what made the manager's
  school detour dead code in the first place.

``vs_rbac.tests.test_reference_scoping`` covers the same two serializer fields
end to end through the API. This module covers the layers underneath it.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.managers import TenantAwareManager
from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
from vs_rbac.serializers.tenant import (
    BRANCH_NOT_FOUND,
    TenantRoleTemplateDetailSerializer,
    TenantUserRoleAssignmentSerializer,
)

from .helpers import make_branch, make_role, make_school, make_staff_user


class _TwoShapedTenants(TestCase):
    """One tenant with several branches, one with none, and a rival to steal from."""

    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="btb-multi", name="BTB Multi")
        cls.hq = make_branch(cls.multi, name="HQ")
        cls.lekki = make_branch(cls.multi, name="Lekki", is_main=False)

        cls.branchless = make_school(slug="btb-solo", name="BTB Solo")

        cls.rival = make_school(slug="btb-rival", name="BTB Rival")
        cls.rival_branch = make_branch(cls.rival, name="Rival Main")

        cls.user = make_staff_user(cls.hq, email="btb.user@test.com")
        cls.role = make_role(cls.multi, name="BTB Role")


class RoleTemplateBranchGuardTests(_TwoShapedTenants):
    """``TenantRoleTemplate.clean`` - models.py, the branch tenancy guard."""

    def test_a_branch_from_another_tenant_is_rejected(self):
        role = TenantRoleTemplate(
            tenant=self.multi.tenant, key="btb-foreign", name="Foreign Branch Role",
            branch=self.rival_branch,
        )
        with self.assertRaises(ValidationError):
            role.clean()

    def test_a_branch_in_the_same_tenant_is_accepted(self):
        role = TenantRoleTemplate(
            tenant=self.multi.tenant, key="btb-own", name="Own Branch Role",
            branch=self.lekki,
        )
        role.clean()  # must not raise

    def test_a_role_without_a_branch_is_accepted(self):
        role = TenantRoleTemplate(
            tenant=self.branchless.tenant, key="btb-none", name="Branchless Role",
        )
        role.clean()  # must not raise

    def test_a_branchless_tenant_cannot_borrow_a_branch(self):
        role = TenantRoleTemplate(
            tenant=self.branchless.tenant, key="btb-borrow", name="Borrowed Branch Role",
            branch=self.hq,
        )
        with self.assertRaises(ValidationError):
            role.clean()

    def test_full_clean_refuses_the_foreign_branch_too(self):
        """``clean()`` is not the only entry point; assert the whole validation
        pass still fails, so nothing can persist a cross-tenant branch."""
        role = TenantRoleTemplate(
            tenant=self.multi.tenant, key="btb-full", name="Full Clean Role",
            branch=self.rival_branch,
        )
        with self.assertRaises(ValidationError):
            role.full_clean()


class AssignmentBranchGuardTests(_TwoShapedTenants):
    """``TenantUserRoleAssignment.clean`` - models.py, the branch tenancy guard."""

    def test_a_branch_from_another_tenant_is_rejected_and_keyed_on_branch(self):
        assignment = TenantUserRoleAssignment(
            tenant=self.multi.tenant, user=self.user, role=self.role,
            branch=self.rival_branch,
        )
        with self.assertRaises(ValidationError) as caught:
            assignment.clean()
        self.assertIn("branch", caught.exception.message_dict)

    def test_a_branch_in_the_same_tenant_is_accepted(self):
        assignment = TenantUserRoleAssignment(
            tenant=self.multi.tenant, user=self.user, role=self.role, branch=self.lekki,
        )
        assignment.clean()  # must not raise

    def test_an_assignment_without_a_branch_is_accepted(self):
        assignment = TenantUserRoleAssignment(
            tenant=self.multi.tenant, user=self.user, role=self.role,
        )
        assignment.clean()  # must not raise


class BranchFieldQuerysetScopingTests(_TwoShapedTenants):
    """``TenantScopedRelatedField.get_queryset`` on both branch fields.

    This is the layer that makes a foreign branch *not exist* rather than be
    rejected afterwards, so it is the one whose ``tenant_lookup`` matters most.
    """

    def _field(self, serializer_class):
        serializer = serializer_class(context={"tenant": self.multi.tenant})
        return serializer.fields["branch"]

    def test_role_branch_field_only_sees_its_own_tenants_branches(self):
        queryset = self._field(TenantRoleTemplateDetailSerializer).get_queryset()
        self.assertCountEqual(list(queryset), [self.hq, self.lekki])
        self.assertNotIn(self.rival_branch, queryset)

    def test_assignment_branch_field_only_sees_its_own_tenants_branches(self):
        queryset = self._field(TenantUserRoleAssignmentSerializer).get_queryset()
        self.assertCountEqual(list(queryset), [self.hq, self.lekki])
        self.assertNotIn(self.rival_branch, queryset)

    def test_a_branchless_tenant_sees_no_branches_at_all(self):
        serializer = TenantRoleTemplateDetailSerializer(context={"tenant": self.branchless.tenant})
        self.assertEqual(list(serializer.fields["branch"].get_queryset()), [])

    def test_the_field_is_not_scoped_by_ambient_context(self):
        """``all_objects`` plus an explicit filter, deliberately: the tenant the
        serializer was given is the boundary, not the request contextvar."""
        from vs_tenants.context import clear_current_tenant, set_current_tenant

        set_current_tenant(self.rival.tenant)
        try:
            queryset = self._field(TenantRoleTemplateDetailSerializer).get_queryset()
            self.assertCountEqual(list(queryset), [self.hq, self.lekki])
        finally:
            clear_current_tenant()


class BranchBackstopValidationTests(_TwoShapedTenants):
    """The ``validate_branch`` / ``validate`` fallbacks.

    Unreachable through the API because the field resolves the branch inside
    the tenant first, so a direct call is the only way they are ever exercised
    and the only way a broken rewrite here would be caught.
    """

    def test_role_serializer_backstop_refuses_a_foreign_branch(self):
        serializer = TenantRoleTemplateDetailSerializer(context={"tenant": self.multi.tenant})
        with self.assertRaises(Exception) as caught:
            serializer.validate_branch(self.rival_branch)
        self.assertIn(BRANCH_NOT_FOUND, str(caught.exception))

    def test_role_serializer_backstop_accepts_an_own_branch(self):
        serializer = TenantRoleTemplateDetailSerializer(context={"tenant": self.multi.tenant})
        self.assertEqual(serializer.validate_branch(self.lekki), self.lekki)

    def test_role_serializer_backstop_accepts_no_branch(self):
        serializer = TenantRoleTemplateDetailSerializer(context={"tenant": self.multi.tenant})
        self.assertIsNone(serializer.validate_branch(None))

    def test_assignment_serializer_backstop_refuses_a_foreign_branch(self):
        serializer = TenantUserRoleAssignmentSerializer(context={"tenant": self.multi.tenant})
        with self.assertRaises(Exception) as caught:
            serializer.validate({
                "user": self.user, "role": self.role, "branch": self.rival_branch,
            })
        self.assertIn(BRANCH_NOT_FOUND, str(caught.exception))

    def test_assignment_serializer_backstop_accepts_an_own_branch(self):
        serializer = TenantUserRoleAssignmentSerializer(context={"tenant": self.multi.tenant})
        attrs = serializer.validate({
            "user": self.user, "role": self.role, "branch": self.lekki,
        })
        self.assertEqual(attrs["branch"], self.lekki)


class TenantLookupInvariantTests(TestCase):
    """Why the manager's school detour is dead, asserted rather than assumed.

    ``TenantAwareManager._tenant_lookup`` prefers a direct ``tenant`` field over
    the ``school`` and ``branch`` fallbacks. ``Branch`` was the last model that
    reached the tenant any other way, and Phase B gave it its own column. If a
    model ever regresses to a school-shaped or branch-shaped ownership path this
    fails, which is the point: the fallbacks are no longer exercised by anything
    and must not quietly start being load-bearing again.
    """

    def test_every_tenant_aware_model_resolves_through_its_own_tenant_column(self):
        from django.apps import apps

        offenders = []
        for model in apps.get_models():
            for name, manager in model._meta.managers_map.items():
                if not isinstance(manager, TenantAwareManager):
                    continue
                lookup = manager._tenant_lookup()
                if lookup != "tenant":
                    offenders.append(f"{model._meta.label}.{name} -> {lookup}")

        self.assertEqual(offenders, [], "a model still reaches its tenant indirectly")

    def test_the_branch_fallback_no_longer_travels_through_the_school(self):
        """If the fallback is ever reached again it must not re-introduce the
        detour: ``branch__tenant``, never ``branch__school__tenant``.

        Forced by binding a manager to the branch path explicitly, because no
        real model takes it any more. The assertion is on the generated SQL: a
        join to the school table is exactly what this phase removed.
        """
        from vs_tenants.context import clear_current_tenant, set_current_tenant

        school = make_school(slug="btb-lookup", name="BTB Lookup")

        manager = TenantAwareManager(tenant_field="branch")
        manager.model = TenantUserRoleAssignment

        set_current_tenant(school.tenant)
        try:
            sql = str(manager.get_queryset().query)
        finally:
            clear_current_tenant()

        self.assertNotIn("vs_schools_school", sql, "the school detour is back")
        self.assertIn("vs_schools_branch", sql)

    def test_the_branch_fallback_still_filters_rather_than_passing_everything(self):
        """A rewritten lookup that names a non-existent path would raise; one
        that names nothing at all would return every row. Prove it narrows."""
        from vs_tenants.context import clear_current_tenant, set_current_tenant

        school = make_school(slug="btb-narrow", name="BTB Narrow")
        branch = make_branch(school, name="Only")
        rival = make_school(slug="btb-narrow-rival", name="BTB Narrow Rival")
        rival_branch = make_branch(rival, name="Rival Only")
        user = make_staff_user(branch, email="btb.narrow@test.com")
        rival_user = make_staff_user(rival_branch, email="btb.narrow.rival@test.com")
        mine = TenantUserRoleAssignment.objects.create(
            tenant=school.tenant, user=user, role=make_role(school, name="Narrow"),
            branch=branch, assignment_status="ACTIVE",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=rival.tenant, user=rival_user,
            role=make_role(rival, name="Narrow Rival"),
            branch=rival_branch, assignment_status="ACTIVE",
        )

        manager = TenantAwareManager(tenant_field="branch")
        manager.model = TenantUserRoleAssignment

        set_current_tenant(school.tenant)
        try:
            self.assertEqual(list(manager.get_queryset()), [mine])
        finally:
            clear_current_tenant()
