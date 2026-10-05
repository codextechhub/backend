"""Finance Admin grows with the finance module in every school that has it.

Corona and Bright Star were created with Finance Admin as the library had it. A
key shipped later, cutting a petty cash float, reaches the library role when the
seed runs, and at that moment each school's own Finance Admin gains it too. What
a school decided stays decided: a key Corona took off its Finance Admin is not put
back, a key it refused stays refused, and its own Bursar role is not touched. Keys
only CodeX may hold never reach a school.
"""
from __future__ import annotations

import datetime
import importlib
from io import StringIO

from django.apps import apps as django_apps
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from vs_rbac.models import Permission, PrebuiltRolePermission, TenantRolePermission
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
)

catch_up_migration = importlib.import_module(
    "vs_rbac.migrations.0032_module_roles_catch_up_with_their_modules")
codex_only_migration = importlib.import_module(
    "vs_rbac.migrations.0031_platform_payment_keys_are_codex_only")


def seed_library(*args):
    call_command("seed_prebuilt_role_templates", *args, stdout=StringIO(), stderr=StringIO())


def held(role):
    return dict(
        TenantRolePermission.objects.filter(role=role).values_list("permission_id", "granted"))


class _Schools(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.view = make_permission("finance.invoice.view")
        cls.taken_off = make_permission("finance.invoice.create")
        cls.refused = make_permission("finance.payment.create")
        seed_library()
        cls.corona = make_school(slug="growth-corona", name="Corona", status="ACTIVE").tenant
        cls.bright = make_school(slug="growth-bright", name="Bright Star", status="ACTIVE").tenant
        cls.corona_fa = make_role(cls.corona, name="Finance Admin", key="finance-admin",
                                  is_system_role=True)
        make_role_permission(cls.corona_fa, cls.view)
        make_role_permission(cls.corona_fa, cls.refused, granted=False)
        cls.bright_fa = make_role(cls.bright, name="Finance Admin", key="finance_admin",
                                  is_system_role=True)
        cls.bursar = make_role(cls.corona, name="Bursar", key="bursar")
        make_role_permission(cls.bursar, cls.view)
        cls.codex_role = make_role(codex_tenant(), name="Growth Platform", key="growth-platform",
                                   is_system_role=True)


class LibraryGrowthTests(_Schools):

    def test_a_key_shipped_later_reaches_every_schools_finance_admin(self):
        new = make_permission("finance.pettycash.return", sensitivity_level="SENSITIVE")

        seed_library()

        self.assertTrue(PrebuiltRolePermission.objects.filter(
            prebuilt_role__key="finance_admin", permission=new).exists())
        self.assertIs(held(self.corona_fa).get(new.key), True)
        self.assertIs(held(self.bright_fa).get(new.key), True)

    def test_what_a_school_decided_stays_decided(self):
        make_permission("finance.pettycash.close")

        seed_library()

        corona = held(self.corona_fa)
        self.assertNotIn(self.taken_off.key, corona)
        self.assertIs(corona[self.refused.key], False)
        self.assertNotIn("finance.pettycash.close", held(self.bursar))
        self.assertNotIn("finance.pettycash.close", held(self.codex_role))

    def test_a_second_run_adds_nothing(self):
        make_permission("finance.pettycash.reopen")
        seed_library()
        before = TenantRolePermission.objects.count()

        seed_library()

        self.assertEqual(TenantRolePermission.objects.count(), before)

    def test_a_key_only_codex_may_hold_never_reaches_a_school(self):
        platform = make_permission("payments.platform_provider.view", scope="PLATFORM")

        seed_library()

        self.assertNotIn(platform.key, held(self.corona_fa))
        self.assertFalse(PrebuiltRolePermission.objects.filter(permission=platform).exists())

    def test_a_reset_of_the_library_does_not_push_every_default_again(self):
        seed_library("--reset")

        self.assertNotIn(self.taken_off.key, held(self.corona_fa))


class CatchUpMigrationTests(_Schools):
    """Keys the copies missed before the seed grew them are granted once."""

    def test_keys_registered_after_the_copy_are_granted_and_older_ones_are_not(self):
        later = make_permission("finance.interbranch.view")
        payments = make_permission("payments.settings.view")
        Permission.objects.filter(key__in=[later.key, payments.key]).update(
            created_at=timezone.now() + datetime.timedelta(minutes=5))
        Permission.objects.filter(key=self.taken_off.key).update(
            created_at=timezone.now() - datetime.timedelta(days=30))

        catch_up_migration.catch_up(django_apps, None)

        corona = held(self.corona_fa)
        self.assertIs(corona.get(later.key), True)
        self.assertIs(corona.get(payments.key), True)
        self.assertNotIn(self.taken_off.key, corona)
        self.assertIs(corona[self.refused.key], False)
        self.assertNotIn(later.key, held(self.bursar))


class PlatformPaymentKeysTests(_Schools):
    """The cross-tenant payment keys become CodeX's and leave every school role."""

    def test_a_school_loses_the_unattributed_webhook_keys_and_codex_keeps_them(self):
        webhook = make_permission("payments.unattributed_webhook.view", scope="TENANT")
        make_role_permission(self.corona_fa, webhook)
        make_role_permission(self.codex_role, webhook)

        codex_only_migration.codex_only(django_apps, None)

        webhook.refresh_from_db()
        self.assertEqual(webhook.scope, "PLATFORM")
        self.assertNotIn(webhook.key, held(self.corona_fa))
        self.assertIs(held(self.codex_role).get(webhook.key), True)
        self.assertEqual(
            Permission.objects.get(key=self.view.key).scope, "TENANT")
