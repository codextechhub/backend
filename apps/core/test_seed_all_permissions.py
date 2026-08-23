from django.test import TestCase

from core.management.commands.seed_all_permissions import Command
from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
)
from vs_rbac.tests.helpers import make_permission
from vs_tenants.models import Tenant


class SuperAdminPermissionReconciliationTests(TestCase):
    def test_super_admin_gets_every_active_permission_without_expanding_platform_admin(self):
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        super_admin, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=codex,
            key="xvs_super_admin",
            defaults={"name": "XVS Super Admin", "is_system_role": True},
        )
        platform_admin, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=codex,
            key="xvs_platform_admin",
            defaults={"name": "XVS Platform Admin", "is_system_role": True},
        )
        first = make_permission("new_module.first.generate")
        second = make_permission("new_module.second.manage")
        TenantRolePermission.objects.create(
            role=super_admin,
            permission=first,
            granted=False,
        )

        Command()._ensure_super_admin_has_every_permission()

        self.assertEqual(
            TenantRolePermission.objects.filter(
                role=super_admin,
                permission__in=(first, second),
                granted=True,
            ).count(),
            2,
        )
        self.assertFalse(
            TenantRolePermission.objects.filter(
                role=platform_admin,
                permission__in=(first, second),
                granted=True,
            ).exists()
        )
