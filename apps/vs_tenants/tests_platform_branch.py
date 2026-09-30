"""The platform tenant keeps a branch, as every tenant does.

CodeX's own books name a branch like any school's: its invoices, journals and
purchases are Lagos's. vs_tenants 0011 gives the platform tenant that branch
when it owns none, and changes nothing when it already owns one.
"""
from importlib import import_module

from django.apps import apps
from django.test import TestCase

from vs_tenants.models import Branch, BranchLifecycle, Tenant

migration = import_module("vs_tenants.migrations.0011_platform_tenant_lagos_branch")


class PlatformTenantBranchTests(TestCase):

    def setUp(self):
        self.codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)

    def test_codex_owns_lagos_as_its_active_main_branch(self):
        branches = list(Branch.all_objects.filter(tenant=self.codex))

        self.assertEqual([(b.name, b.code, b.is_main, b.status) for b in branches],
                         [("Lagos", 1, True, "ACTIVE")])
        self.assertIsNotNone(branches[0].activated_at)
        event = BranchLifecycle.objects.get(branch=branches[0])
        self.assertEqual((event.from_state, event.to_state, event.actor_id), ("PENDING", "ACTIVE", ""))

    def test_running_it_again_adds_nothing(self):
        migration.create_lagos_branch(apps, None)

        self.assertEqual(Branch.all_objects.filter(tenant=self.codex).count(), 1)
        self.assertEqual(BranchLifecycle.objects.filter(branch__tenant=self.codex).count(), 1)
