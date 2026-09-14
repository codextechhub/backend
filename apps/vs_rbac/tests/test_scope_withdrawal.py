"""Reclassifying a key must take back every grant of it, on every surface.

Making a key ``PLATFORM`` and leaving the old grants behind looks harmless:
:func:`vs_rbac.evaluator.get_effective_permissions` filters them out, so nobody
gains anything and nothing in the UI changes. It bites on the next *write*
through the surface the leftover sits on, and the surface nobody swept was the
prebuilt role library. Creating a school provisions Finance Admin by copying the
library's defaults into the new school's own role, so one stale row there made
**creating a school fail outright**, naming a permission the person creating the
school had never heard of.

These tests build that exact shape - rows written while the key was tenant-safe,
then the scope flipped by a queryset update the way a migration does it - and
then assert both halves: that the breakage is real, and that
:func:`vs_rbac.scope_withdrawal.withdraw_from_tenants` ends it.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.models import (
    GroupPermission,
    Permission,
    PermissionGroup,
    PermissionScope,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
    UserPermissionOverride,
)
from vs_rbac.scope_withdrawal import withdraw_from_tenants
from vs_rbac.services import provision_role_from_prebuilt
from vs_tenants.models import Tenant

from .helpers import (
    codex_tenant,
    make_branch,
    make_permission,
    make_role,
    make_school,
    make_school_admin,
)


ENTITY_CREATE = "finance.entity.create"
OTHER_KEY = "finance.invoice.view"


def reclassify(key):
    """Flip a key to PLATFORM the way a migration does: a queryset update.

    Going through ``save()`` would run the guards, and the whole point is that
    the rows already written are not re-validated when the registry changes
    under them.
    """
    Permission.objects.filter(key=key).update(scope=PermissionScope.PLATFORM)


class LibraryLeftoverBreaksTenantProvisioningTests(TestCase):
    """The failure a school administrator actually saw."""

    def setUp(self):
        self.school = make_school(slug="bright-star", name="Bright Star School")
        self.tenant = self.school.tenant
        self.entity_create = make_permission(ENTITY_CREATE)
        self.invoice_view = make_permission(OTHER_KEY)

        # The migrations ship this template, so the fixture adopts the real
        # library row rather than a second one the unique key would refuse.
        self.template, _ = PrebuiltRoleTemplate.objects.get_or_create(
            key="finance_admin",
            defaults={"name": "Finance Admin", "scope": "institution", "tier": "A"},
        )
        for permission in (self.entity_create, self.invoice_view):
            PrebuiltRolePermission.objects.create(
                prebuilt_role=self.template, permission=permission,
            )
        reclassify(ENTITY_CREATE)

    def test_provisioning_survives_a_stale_library_and_drops_the_key(self):
        """Creating a school must not die over a key it never asked for."""
        with self.assertLogs("vs_rbac.services", level="WARNING") as logged:
            role = provision_role_from_prebuilt(
                tenant=self.tenant, branch=None, prebuilt_key="finance_admin",
            )

        self.assertIn(ENTITY_CREATE, "\n".join(logged.output))
        granted = set(
            TenantRolePermission.objects
            .filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(granted, {OTHER_KEY})

    def test_the_guard_still_refuses_a_platform_key_written_directly(self):
        """Dropping it on the copy is not the same as allowing it anywhere."""
        role = make_role(self.school, name="Bursar")
        with self.assertRaises(ValidationError) as caught:
            TenantRolePermission.objects.create(
                role=role, permission=self.entity_create,
            )
        self.assertIn(ENTITY_CREATE, str(caught.exception))

    def test_withdrawal_lets_the_school_have_its_finance_admin(self):
        withdraw_from_tenants([ENTITY_CREATE])

        role = provision_role_from_prebuilt(
            tenant=self.tenant, branch=None, prebuilt_key="finance_admin",
        )

        granted = set(
            TenantRolePermission.objects
            .filter(role=role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(granted, {OTHER_KEY})

    def test_the_library_keeps_every_key_a_tenant_may_still_hold(self):
        withdraw_from_tenants([ENTITY_CREATE])

        held = set(
            PrebuiltRolePermission.objects
            .filter(prebuilt_role=self.template)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(held, {OTHER_KEY})


class WithdrawalReachesEverySurfaceTests(TestCase):
    """Four surfaces can hold a grant, and a sweep that misses one is a bug."""

    def setUp(self):
        self.school = make_school(slug="greenfield", name="Greenfield School")
        self.branch = make_branch(self.school)
        self.permission = make_permission(ENTITY_CREATE)

        self.role = make_role(self.school, name="Bursar")
        TenantRolePermission.objects.create(role=self.role, permission=self.permission)

        # The migrations ship this template, so the fixture adopts the real
        # library row rather than a second one the unique key would refuse.
        self.template, _ = PrebuiltRoleTemplate.objects.get_or_create(
            key="finance_admin",
            defaults={"name": "Finance Admin", "scope": "institution", "tier": "A"},
        )
        PrebuiltRolePermission.objects.create(
            prebuilt_role=self.template, permission=self.permission,
        )

        self.group = PermissionGroup.objects.create(
            name="Finance - all", scope=PermissionScope.TENANT,
        )
        GroupPermission.objects.create(group=self.group, permission=self.permission)

        self.user = make_school_admin(self.branch, email="bursar@greenfield.test")
        UserPermissionOverride.objects.create(
            tenant=self.school.tenant, user=self.user, permission=self.permission,
            mode=UserPermissionOverride.Mode.ALLOW, reason="Covering month end.",
        )

        reclassify(ENTITY_CREATE)

    def test_every_tenant_side_grant_goes(self):
        counts = withdraw_from_tenants([ENTITY_CREATE])

        self.assertEqual(counts, {
            "role_permissions": 1,
            "prebuilt_defaults": 1,
            "group_memberships": 1,
            "overrides": 1,
        })
        self.assertFalse(TenantRolePermission.objects.filter(
            permission_id=ENTITY_CREATE).exists())
        self.assertFalse(PrebuiltRolePermission.objects.filter(
            permission_id=ENTITY_CREATE).exists())
        self.assertFalse(GroupPermission.objects.filter(
            permission_id=ENTITY_CREATE).exists())
        self.assertFalse(UserPermissionOverride.objects.filter(
            permission_id=ENTITY_CREATE).exists())

    def test_called_with_no_keys_it_sweeps_whatever_the_registry_refuses(self):
        withdraw_from_tenants()

        self.assertFalse(TenantRolePermission.objects.filter(
            permission_id=ENTITY_CREATE).exists())
        self.assertFalse(PrebuiltRolePermission.objects.filter(
            permission_id=ENTITY_CREATE).exists())


class WithdrawalSparesWhatItShouldTests(TestCase):
    """A sweep that takes too much is as wrong as one that takes too little."""

    def setUp(self):
        self.permission = make_permission(ENTITY_CREATE)
        self.codex = codex_tenant()

    def test_a_platform_tenants_own_role_keeps_the_key(self):
        role = make_role(self.codex, name="CX Finance")
        TenantRolePermission.objects.create(role=role, permission=self.permission)
        reclassify(ENTITY_CREATE)

        withdraw_from_tenants([ENTITY_CREATE])

        self.assertTrue(TenantRolePermission.objects.filter(
            role=role, permission_id=ENTITY_CREATE).exists())

    def test_a_platform_group_keeps_the_key(self):
        group = PermissionGroup.objects.create(
            name="CX Finance - all", scope=PermissionScope.PLATFORM,
        )
        GroupPermission.objects.create(group=group, permission=self.permission)
        reclassify(ENTITY_CREATE)

        withdraw_from_tenants([ENTITY_CREATE])

        self.assertTrue(GroupPermission.objects.filter(
            group=group, permission_id=ENTITY_CREATE).exists())

    def test_a_deny_override_stays(self):
        school = make_school(slug="deny-school", name="Deny School")
        branch = make_branch(school)
        user = make_school_admin(branch, email="denied@deny.test")
        UserPermissionOverride.objects.create(
            tenant=school.tenant, user=user, permission=self.permission,
            mode=UserPermissionOverride.Mode.DENY, reason="Left the finance team.",
        )
        reclassify(ENTITY_CREATE)

        withdraw_from_tenants([ENTITY_CREATE])

        self.assertTrue(UserPermissionOverride.objects.filter(
            user=user, permission_id=ENTITY_CREATE,
            mode=UserPermissionOverride.Mode.DENY).exists())


class WithdrawalReachesAnyTenantKindTests(TestCase):
    """The sweep asks "is this the platform", not "is this a school".

    ``vs_health`` runs on ``ORGANIZATION`` tenants, and a sweep written as
    ``kind="SCHOOL"`` walks straight past them - leaving a hospital with exactly
    the stale row that stops its roles being saved.
    """

    def test_an_organization_tenant_loses_the_key_too(self):
        tenant = Tenant.objects.create(
            name="Lagoon Hospital", slug="lagoon-hospital",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        permission = make_permission(ENTITY_CREATE)
        role = make_role(tenant, name="Finance Lead")
        TenantRolePermission.objects.create(role=role, permission=permission)
        reclassify(ENTITY_CREATE)

        withdraw_from_tenants([ENTITY_CREATE])

        self.assertFalse(TenantRolePermission.objects.filter(
            permission_id=ENTITY_CREATE).exists())
