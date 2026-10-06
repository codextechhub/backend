"""Finance's write resolvers read a JSON number as an id, never as a code.

Every resolver here accepts a row by code or by id. Ids are one sequence shared
by every school's books, so on a busy database an id reaches the digits another
row of the same books wears as its code: account 5100 meets the account coded
"5100". Each test builds that collision on purpose, with a decoy coded with the
target's id, and sends the target's id as a number.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.test import TestCase

from vs_finance.models import Account, CostCenter, Customer, LedgerEntity, TaxCode
from vs_finance.seed import seed_chart_of_accounts
from vs_finance.views_ar import _resolve_customer
from vs_finance.views_ops.base import _resolve_account, _resolve_cost_center, _resolve_tax


class CodeOrIdResolverTests(TestCase):
    """One entity's books, and a request whose caller nothing narrows."""

    @classmethod
    def setUpTestData(cls):
        cls.entity = LedgerEntity.objects.create(
            name="Corona Books", code="CORONA", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(cls.entity)

    def setUp(self):
        self.request = SimpleNamespace(user=None)

    def test_an_account_id_is_not_read_as_another_accounts_code(self):
        target = Account.objects.create(
            entity=self.entity, code="1410", name="Alternate inventory",
            account_type="ASSET", is_postable=True,
        )
        decoy = Account.objects.create(
            entity=self.entity, code=str(target.pk), name="Decoy expense",
            account_type="EXPENSE", is_postable=True,
        )

        self.assertEqual(_resolve_account(self.request, self.entity, target.pk, "a"), target)
        self.assertEqual(
            _resolve_account(self.request, self.entity, str(target.pk), "a"), decoy,
        )

    def test_a_cost_centre_id_is_not_read_as_another_centres_code(self):
        target = CostCenter.objects.create(entity=self.entity, code="ADMIN", name="Admin")
        decoy = CostCenter.objects.create(
            entity=self.entity, code=str(target.pk), name="Decoy",
        )

        self.assertEqual(_resolve_cost_center(self.entity, target.pk), target)
        self.assertEqual(_resolve_cost_center(self.entity, str(target.pk)), decoy)

    def test_a_tax_code_id_is_not_read_as_another_codes_code(self):
        target = TaxCode.objects.create(entity=self.entity, code="VAT", name="VAT", rate_bps=750)
        decoy = TaxCode.objects.create(
            entity=self.entity, code=str(target.pk), name="Decoy", rate_bps=0,
        )

        self.assertEqual(_resolve_tax(self.entity, target.pk), target)
        self.assertEqual(_resolve_tax(self.entity, str(target.pk)), decoy)

    def test_a_customer_id_is_not_read_as_another_customers_code(self):
        receivable = Account.objects.get(entity=self.entity, code="1200")
        target = Customer.objects.create(
            entity=self.entity, code="ADA", name="Ada Okeke", receivable_account=receivable,
        )
        decoy = Customer.objects.create(
            entity=self.entity, code=str(target.pk), name="Decoy family",
            receivable_account=receivable,
        )

        self.assertEqual(_resolve_customer(self.request, self.entity, target.pk), target)
        self.assertEqual(_resolve_customer(self.request, self.entity, str(target.pk)), decoy)
        self.assertEqual(_resolve_customer(self.request, self.entity, "ada"), target)
