"""Backend dependency seeding and retirement of legacy system groups."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.models import (
    PermissionDependency,
    PermissionGroup,
    PermissionScope,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRoleGroup,
    TenantRolePermission,
)
from vs_rbac.tests.helpers import (
    make_dependency,
    make_permission,
    make_role,
    make_role_group,
    make_role_permission,
    make_school,
)


class SeedPermissionDependenciesTests(TestCase):
    def test_command_reconciles_dependencies_and_backfills_role_defaults(self):
        view = make_permission("finance.invoice.view")
        create = make_permission("finance.invoice.create")
        update = make_permission("finance.invoice.update")
        delete = make_permission("finance.invoice.delete")
        stale = make_dependency(view.key, update.key)

        tenant = make_school().tenant
        role = make_role(tenant, name="Invoice Editor")
        make_role_permission(role, update)
        prebuilt = PrebuiltRoleTemplate.objects.create(
            key="invoice_manager",
            name="Invoice Manager",
            scope="institution",
        )
        PrebuiltRolePermission.objects.create(
            prebuilt_role=prebuilt,
            permission=delete,
        )

        call_command("seed_permission_dependencies", stdout=StringIO())

        pairs = set(
            PermissionDependency.objects.values_list(
                "permission_id", "depends_on_id",
            ),
        )
        self.assertNotIn((stale.permission_id, stale.depends_on_id), pairs)
        self.assertIn((update.key, view.key), pairs)
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=role, permission=view, granted=True,
            ).exists(),
        )
        self.assertEqual(
            set(
                PrebuiltRolePermission.objects.filter(
                    prebuilt_role=prebuilt,
                ).values_list("permission_id", flat=True),
            ),
            {view.key, delete.key},
        )

        before = set(pairs)
        call_command("seed_permission_dependencies", stdout=StringIO())
        self.assertEqual(
            set(
                PermissionDependency.objects.values_list(
                    "permission_id", "depends_on_id",
                ),
            ),
            before,
        )

    def test_action_without_a_view_sibling_stays_independent(self):
        run = make_permission("platform.task.run", scope=PermissionScope.PLATFORM)

        call_command("seed_permission_dependencies", stdout=StringIO())

        self.assertFalse(
            PermissionDependency.objects.filter(permission=run).exists(),
        )

    def test_group_updates_require_registry_view_access(self):
        registry_view = make_permission(
            "platform.permissions.view", scope=PermissionScope.PLATFORM,
        )
        group_update = make_permission(
            "platform.permission_groups.update", scope=PermissionScope.PLATFORM,
        )

        call_command("seed_permission_dependencies", stdout=StringIO())

        self.assertTrue(
            PermissionDependency.objects.filter(
                permission=group_update,
                depends_on=registry_view,
            ).exists(),
        )


class RetireSystemPermissionGroupsTests(TestCase):
    def test_system_groups_become_direct_grants_before_deletion(self):
        view = make_permission("finance.invoice.view")
        update = make_permission("finance.invoice.update")
        tenant = make_school().tenant
        role = make_role(tenant, name="Invoice Clerk", version=4)
        make_role_permission(role, update, granted=False)
        system_group = PermissionGroup.objects.create(
            name="Legacy Invoice Work",
            scope=PermissionScope.TENANT,
            is_system=True,
        )
        system_group.permissions.add(view, update)
        make_role_group(role, system_group)
        custom_group = PermissionGroup.objects.create(
            name="Administrator Group",
            scope=PermissionScope.TENANT,
            is_system=False,
        )

        call_command("retire_system_permission_groups", stdout=StringIO())

        self.assertFalse(PermissionGroup.objects.filter(pk=system_group.pk).exists())
        self.assertTrue(PermissionGroup.objects.filter(pk=custom_group.pk).exists())
        self.assertFalse(TenantRoleGroup.objects.filter(role=role).exists())
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=role, permission=view, granted=True,
            ).exists(),
        )
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=role, permission=update, granted=False,
            ).exists(),
        )
        role.refresh_from_db()
        self.assertEqual(role.version, 5)
