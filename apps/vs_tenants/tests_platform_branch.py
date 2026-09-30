"""The platform tenant keeps a branch, as every tenant does.

CodeX's own books name a branch like any school's: its invoices, journals and
purchases are Lagos's. vs_tenants 0011 gives the platform tenant that branch
when it owns none, and changes nothing when it already owns one.
"""
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
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


class UndoingThePlatformBranchTests(TestCase):
    """Undoing 0011 removes Lagos only while nothing names it.

    A freshly built database, or a rewind past vs_schools 0004, has nothing on
    the branch, and it goes. A database where CodeX's store belongs to Lagos
    keeps it: removing it would leave that store pointing nowhere.
    """

    def setUp(self):
        self.codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.lagos = Branch.all_objects.get(tenant=self.codex)
        self.schema_editor = SimpleNamespace(connection=connection)

    def test_an_unused_lagos_is_removed(self):
        migration.remove_unused_lagos_branch(apps, self.schema_editor)

        self.assertFalse(Branch.all_objects.filter(tenant=self.codex).exists())

    def test_a_lagos_that_something_names_stays(self):
        from vs_finance.models import LedgerEntity
        from vs_procurement.models import StockLocation

        StockLocation.objects.create(
            entity=LedgerEntity.objects.get(code="CODEX"), branch=self.lagos,
            code="LAGOS-MAIN", name="Lagos store",
        )

        migration.remove_unused_lagos_branch(apps, self.schema_editor)

        self.assertTrue(Branch.all_objects.filter(pk=self.lagos.pk).exists())
