from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from vs_finance.constants import AccountType
from vs_finance.models import Account, Currency, LedgerEntity
from vs_tenants.models import Tenant


class DeleteEntityCommandTests(TestCase):
    def setUp(self):
        self.currency, _ = Currency.objects.get_or_create(
            code="NGN",
            defaults={"name": "Nigerian naira", "symbol": "N"},
        )
        self.tenant = Tenant.objects.create(
            name="Delete Me Ltd",
            slug="delete-me-ltd",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        self.entity = LedgerEntity.objects.create(
            name="Delete Me Books",
            code="DELME",
            tenant=self.tenant,
            base_currency=self.currency,
        )

    def _account(self, *, entity=None, code, parent=None):
        return Account.objects.create(
            entity=entity or self.entity,
            parent=parent,
            code=code,
            name=code,
            account_type=AccountType.ASSET,
        )

    def test_force_deletes_entity_and_nested_protected_rows_only(self):
        parent = self._account(code="1000")
        self._account(code="1010", parent=parent)
        other_tenant = Tenant.objects.create(
            name="Keep Me Ltd",
            slug="keep-me-ltd",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        other_entity = LedgerEntity.objects.create(
            name="Keep Me Books",
            code="KEEP",
            tenant=other_tenant,
            base_currency=self.currency,
        )
        other_account = self._account(entity=other_entity, code="1000")

        call_command("delete_entity", entity=["delme"], force=True, stdout=StringIO())

        self.assertFalse(LedgerEntity.objects.filter(pk=self.entity.pk).exists())
        self.assertFalse(Account.objects.filter(entity_id=self.entity.pk).exists())
        self.assertTrue(LedgerEntity.objects.filter(pk=other_entity.pk).exists())
        self.assertTrue(Account.objects.filter(pk=other_account.pk).exists())
        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())
        self.assertTrue(Currency.objects.filter(pk=self.currency.pk).exists())

    def test_confirmation_must_be_exactly_yes(self):
        output = StringIO()

        with mock.patch("builtins.input", return_value="yes"):
            call_command("delete_entity", entity=["DELME"], stdout=output)

        self.assertTrue(LedgerEntity.objects.filter(pk=self.entity.pk).exists())
        self.assertIn("Aborted.", output.getvalue())

    def test_multiple_codes_delete_found_entities_and_report_missing(self):
        second = LedgerEntity.objects.create(
            name="Second Books",
            code="SECOND",
            tenant=self.tenant,
            base_currency=self.currency,
        )
        output = StringIO()

        call_command(
            "delete_entity",
            entity=["DELME", "missing", "second"],
            force=True,
            stdout=output,
        )

        self.assertFalse(
            LedgerEntity.objects.filter(pk__in=[self.entity.pk, second.pk]).exists()
        )
        self.assertIn("MISSING", output.getvalue())
        self.assertIn("not found (skipped)", output.getvalue())

    def test_no_matching_codes_raises_command_error(self):
        with self.assertRaisesMessage(
            CommandError, "None of the provided entity codes were found."
        ):
            call_command(
                "delete_entity", entity=["MISSING"], force=True, stdout=StringIO()
            )

        self.assertTrue(LedgerEntity.objects.filter(pk=self.entity.pk).exists())
