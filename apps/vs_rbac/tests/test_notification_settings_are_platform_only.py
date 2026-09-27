"""The notification settings key belongs to the platform, and nobody else keeps it.

``communication.communication_permissions.enforce`` gates the matrix deciding
which events send email for every tenant. Migration 0030 makes it ``PLATFORM``
and takes back every grant of it outside the platform tenant. These tests build
the shape the seed left behind (the key ``TENANT``-scoped, granted to the
``school_admin`` and ``branch_admin`` prebuilts and to the tenant roles built
from them) and run the migration's own functions over it, the way the 0008
tests do.

Two tenant shapes are covered: an XVS tenant with the seeded admin roles, one of
them suffixed with a branch id, and an organization tenant with a role of its
own. The sweep asks "is this the platform", not "is this an XVS tenant".
"""
from importlib import import_module

from django.apps import apps as registry
from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.models import (
    Permission,
    PermissionRegistryRevision,
    PermissionScope,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRoleChangeDeltaItem,
    TenantRoleChangeRequest,
    TenantRolePermission,
    UserPermissionOverride,
)
from vs_tenants.models import Tenant

from .helpers import (
    codex_tenant,
    make_branch,
    make_permission,
    make_role,
    make_school,
    make_school_admin,
)

KEY = "communication.communication_permissions.enforce"
MIGRATION = "vs_rbac.migrations.0030_notification_settings_belong_to_the_platform"


def _held_by(role):
    return TenantRolePermission.objects.filter(
        role=role, permission_id=KEY, granted=True,
    ).exists()


class NotificationSettingsKeyMigrationTests(TestCase):
    """Forward takes the key back from every tenant; reverse restores the seed."""

    def setUp(self):
        self.migration = import_module(MIGRATION)
        self.permission = make_permission(KEY, scope=PermissionScope.TENANT)

        self.prebuilts = [
            PrebuiltRoleTemplate.objects.get_or_create(
                key=key, defaults={"name": key.replace("_", " ").title(), "scope": scope},
            )[0]
            for key, scope in (("school_admin", "institution"), ("branch_admin", "branch"))
        ]
        for prebuilt in self.prebuilts:
            PrebuiltRolePermission.objects.get_or_create(
                prebuilt_role=prebuilt, permission=self.permission,
            )

        self.xvs = make_school(slug="bright-star", name="Bright Star")
        self.branch = make_branch(self.xvs)
        self.tenant_admin = make_role(
            self.xvs, name="Admin", key="school_admin", is_system_role=True,
        )
        self.branch_admin = make_role(
            self.xvs, name="Branch Admin", key=f"branch_admin-{self.branch.pk}",
            is_system_role=True,
        )
        self.custom = make_role(self.xvs, name="Communications Lead")
        for role in (self.tenant_admin, self.branch_admin, self.custom):
            TenantRolePermission.objects.create(role=role, permission=self.permission)

        self.organization = Tenant.objects.create(
            name="Lagoon Hospital", slug="lagoon-hospital",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.organization_role = make_role(self.organization, name="Comms")
        TenantRolePermission.objects.create(
            role=self.organization_role, permission=self.permission,
        )

        self.platform_role = make_role(codex_tenant(), name="Platform Comms")
        TenantRolePermission.objects.create(
            role=self.platform_role, permission=self.permission,
        )

        self.person = make_school_admin(self.branch, email="bursar@bright-star.test")
        self.allow = UserPermissionOverride.objects.create(
            tenant=self.xvs.tenant, user=self.person, permission=self.permission,
            mode=UserPermissionOverride.Mode.ALLOW, reason="Covering the admin.",
        )
        self.deny = UserPermissionOverride.objects.create(
            tenant=self.xvs.tenant,
            user=make_school_admin(self.branch, email="clerk@bright-star.test"),
            permission=self.permission,
            mode=UserPermissionOverride.Mode.DENY, reason="Not this one.",
        )

        self.pending_add = self._role_change(TenantRoleChangeRequest.Status.PENDING)
        self.approved_add = self._role_change(TenantRoleChangeRequest.Status.APPROVED)

    def _role_change(self, status):
        request = TenantRoleChangeRequest.objects.create(
            tenant=self.xvs.tenant, requested_by=self.person,
            target_role=self.custom, justification="Needs the matrix.",
            status=status,
        )
        return TenantRoleChangeDeltaItem.objects.create(
            request=request, permission=self.permission,
            operation=TenantRoleChangeDeltaItem.Operation.ADD,
        )

    def test_forward_makes_the_key_platform_scoped(self):
        self.migration.forward(registry, None)

        self.permission.refresh_from_db()
        self.assertEqual(self.permission.scope, PermissionScope.PLATFORM)

    def test_forward_takes_every_tenant_grant_and_default(self):
        self.migration.forward(registry, None)

        for role in (self.tenant_admin, self.branch_admin, self.custom,
                     self.organization_role):
            self.assertFalse(_held_by(role), role.key)
        self.assertFalse(
            PrebuiltRolePermission.objects.filter(permission_id=KEY).exists(),
        )
        self.assertFalse(
            UserPermissionOverride.objects.filter(pk=self.allow.pk).exists(),
        )
        self.assertFalse(
            TenantRoleChangeDeltaItem.objects.filter(pk=self.pending_add.pk).exists(),
        )

    def test_forward_keeps_the_platform_a_deny_and_decided_history(self):
        self.migration.forward(registry, None)

        self.assertTrue(_held_by(self.platform_role))
        self.assertTrue(UserPermissionOverride.objects.filter(pk=self.deny.pk).exists())
        self.assertTrue(
            TenantRoleChangeDeltaItem.objects.filter(pk=self.approved_add.pk).exists(),
        )

    def test_forward_bumps_the_registry_revision(self):
        before = PermissionRegistryRevision.current()
        self.migration.forward(registry, None)
        self.assertEqual(PermissionRegistryRevision.current(), before + 1)

    def test_after_forward_a_tenant_role_cannot_be_given_the_key(self):
        self.migration.forward(registry, None)

        with self.assertRaises(ValidationError) as caught:
            TenantRolePermission.objects.create(
                role=self.tenant_admin, permission=self.permission,
            )
        self.assertIn(KEY, str(caught.exception))

    def test_reverse_restores_the_seeded_grants_and_nothing_else(self):
        self.migration.forward(registry, None)
        self.migration.backward(registry, None)

        self.permission.refresh_from_db()
        self.assertEqual(self.permission.scope, PermissionScope.TENANT)
        self.assertTrue(_held_by(self.tenant_admin))
        self.assertTrue(_held_by(self.branch_admin))
        self.assertEqual(
            set(PrebuiltRolePermission.objects.filter(permission_id=KEY)
                .values_list("prebuilt_role__key", flat=True)),
            {"school_admin", "branch_admin"},
        )
        # Grants nobody can prove existed stay gone.
        self.assertFalse(_held_by(self.custom))
        self.assertFalse(_held_by(self.organization_role))
        self.assertFalse(
            UserPermissionOverride.objects.filter(
                permission_id=KEY, mode=UserPermissionOverride.Mode.ALLOW,
            ).exists(),
        )
        self.assertTrue(_held_by(self.platform_role))


class NotificationSettingsKeyMissingTests(TestCase):
    """A fresh database runs the migration before the seed creates the key."""

    def test_both_directions_are_harmless_without_the_key(self):
        migration = import_module(MIGRATION)
        self.assertFalse(Permission.objects.filter(key=KEY).exists())

        migration.forward(registry, None)
        migration.backward(registry, None)

        self.assertFalse(Permission.objects.filter(key=KEY).exists())
