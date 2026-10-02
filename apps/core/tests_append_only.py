"""The audit trails stay append-only at the database, and nothing quietly undoes that.

Each table in :data:`core.append_only.APPEND_ONLY_TRIGGERS` carries triggers
that refuse UPDATE and DELETE whatever issued them. These tests fail if a later
migration drops or disables one, and if any migration other than the one that
installs a trigger names it in a DROP.
"""
from __future__ import annotations

import pathlib
import re

from django.apps import apps
from django.db import connection, transaction
from django.db.utils import Error as DatabaseError
from django.test import SimpleTestCase, TestCase

from core.append_only import APPEND_ONLY_TRIGGERS, INSTALLING_MIGRATIONS


class TriggersInPlaceTests(TestCase):
    """Every listed trigger exists on its table and is enabled."""

    def test_every_append_only_trigger_is_installed_and_enabled(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname, t.tgname, t.tgenabled
                FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                WHERE NOT t.tgisinternal
                """
            )
            found = {(table, name): enabled for table, name, enabled in cursor.fetchall()}
        missing, disabled = [], []
        for table, names in APPEND_ONLY_TRIGGERS.items():
            for name in names:
                state = found.get((table, name))
                if state is None:
                    missing.append(f"{table}.{name}")
                elif state == "D":
                    disabled.append(f"{table}.{name}")
        self.assertEqual(missing, [], "append-only triggers missing")
        self.assertEqual(disabled, [], "append-only triggers disabled")


class PlatformTrailTests(TestCase):
    """The platform trail refuses a queryset write, except its two narrow exceptions."""

    @classmethod
    def setUpTestData(cls):
        from vs_audit.models import AuditEvent
        from vs_tenants.models import Tenant

        cls.bright_star = Tenant.objects.create(
            name="Bright Star", slug="bright-star-trail", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        cls.greenfield = Tenant.objects.create(
            name="Greenfield", slug="greenfield-trail", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        for tenant in (cls.bright_star, cls.greenfield):
            AuditEvent.objects.create(
                module_key="FINANCE", action_type="FINANCIAL_TRANSACTION",
                actor_type="SYSTEM", actor_label="test", tenant=tenant,
                entity_type="Invoice", entity_id="1", summary="Invoice posted.",
            )

    def test_update_and_delete_are_refused(self):
        from vs_audit.models import AuditEvent

        rows = AuditEvent.objects.filter(tenant=self.bright_star)
        with self.assertRaises(DatabaseError), transaction.atomic():
            rows.update(summary="tampered")
        with self.assertRaises(DatabaseError), transaction.atomic():
            rows.delete()
        self.assertEqual(rows.get().summary, "Invoice posted.")

    def test_discarding_an_unused_tenants_trail_reaches_that_tenant_only(self):
        from vs_audit.models import AuditEvent
        from vs_audit.services import discard_trail_of_unused_tenant

        with transaction.atomic():
            self.assertEqual(discard_trail_of_unused_tenant(self.bright_star), 1)
            with self.assertRaises(DatabaseError), transaction.atomic():
                AuditEvent.objects.filter(tenant=self.greenfield).delete()
        self.assertFalse(AuditEvent.objects.filter(tenant=self.bright_star).exists())
        self.assertTrue(AuditEvent.objects.filter(tenant=self.greenfield).exists())
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(tenant=self.greenfield).delete()


class NoMigrationDropsTheTriggersTests(SimpleTestCase):
    """Only the migration that installs a trigger may name it in a DROP."""

    def test_no_other_migration_drops_or_disables_an_append_only_trigger(self):
        names = {name for pair in APPEND_ONLY_TRIGGERS.values() for name in pair}
        tables = set(APPEND_ONLY_TRIGGERS)
        drop = re.compile(r"DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
        disable = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+DISABLE\s+TRIGGER", re.IGNORECASE)
        offenders = []
        for config in apps.get_app_configs():
            folder = pathlib.Path(config.path) / "migrations"
            if not folder.is_dir():
                continue
            for path in folder.glob("[0-9]*.py"):
                text = path.read_text()
                allowed = (config.label, path.stem.split("_", 1)[-1]) in INSTALLING_MIGRATIONS
                dropped = {m for m in drop.findall(text) if m in names}
                disabled = {m for m in disable.findall(text) if m in tables}
                if disabled or (dropped and not allowed):
                    offenders.append(f"{config.label}/{path.name}")
        self.assertEqual(offenders, [])
