"""Closed-figure screens and permission pickers use the accountant's words."""
from datetime import date
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.apps import apps as django_apps
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase


class ClosedFiguresChecklistWordsTests(SimpleTestCase):
    def test_the_close_check_names_closed_periods_and_years(self):
        from .seals import sealed_figures_close_check

        period = SimpleNamespace(fiscal_year=SimpleNamespace(start_date=date(2027, 1, 1)))
        years = mock.MagicMock()
        years.order_by.return_value.__getitem__.return_value = []
        result = SimpleNamespace(ok=True, checks=[object(), object()])

        with mock.patch("vs_finance.models.FiscalYear.objects.filter", return_value=years), \
                mock.patch("vs_finance.seals.verify_entity", return_value=result):
            item = sealed_figures_close_check(object(), period)

        self.assertEqual(item.detail, "2 closed periods and years still match the ledger")


class ClosedFiguresPermissionWordsTests(TestCase):
    def test_the_finance_seed_creates_the_permission_description(self):
        from vs_rbac.models import Permission

        Permission.objects.filter(key="finance.seal.view").delete()
        output = StringIO()
        call_command("seed_finance_permissions", stdout=output, verbosity=0)
        permission = Permission.objects.get(key="finance.seal.view")

        self.assertEqual(permission.description, "View closed period figures.")

    def test_the_migration_refreshes_existing_registry_wording(self):
        import importlib

        from vs_rbac.models import Permission, PermissionAction

        output = StringIO()
        call_command("seed_actions", stdout=output, verbosity=0)
        call_command("seed_finance_permissions", stdout=output, verbosity=0)
        permission = Permission.objects.get(key="finance.seal.view")
        permission.description = "View sealed period figures."
        permission.save(update_fields=["description"])
        action = PermissionAction.objects.get(name="lock")
        action.description = "Permanently seal a closed accounting period against any re-open."
        action.save(update_fields=["description"])

        migration = importlib.import_module("vs_rbac.migrations.0033_closed_figures_wording")
        migration.use_closed_figures_wording(django_apps, None)

        permission.refresh_from_db()
        action.refresh_from_db()
        self.assertEqual(permission.description, "View closed period figures.")
        self.assertEqual(
            action.description,
            "Permanently lock a closed accounting period against reopening.",
        )
