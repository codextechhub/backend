"""The write-off list names the account a write-off lands in by default.

Mrs Bello, a bursar at Corona, may raise write-offs but cannot open the settings
screen, so she could not learn which bad-debt account the form falls back to.
The write-off list tells her, and tells nobody who cannot create write-offs.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.constants import AccountMappingKey
from vs_finance.models import Account, FinanceAccountMapping

from .tests_branch_scope import _FinanceBranchFixture


class WriteOffDefaultAccountTests(_FinanceBranchFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.maker = cls.grant(
            cls.user_for(cls.tenant, "maker@corona.test"),
            "finance.writeoff.view", "finance.writeoff.create",
            tenant=cls.tenant, role_key="writeoff-maker")
        cls.reader = cls.grant(
            cls.user_for(cls.tenant, "reader@corona.test"), "finance.writeoff.view",
            tenant=cls.tenant, role_key="writeoff-reader")
        cls.solo_maker = cls.grant(
            cls.user_for(cls.solo_tenant, "maker@single.test"),
            "finance.writeoff.view", "finance.writeoff.create",
            tenant=cls.solo_tenant, role_key="solo-writeoff-maker")

    def fetch(self, user, entity):
        return TenantAPIClient(user=user).get(f"/v1/finance/write-offs/?entity={entity.code}")

    def test_a_maker_reads_the_default_bad_debt_account_in_both_shapes_of_school(self):
        for user, books in ((self.maker, self.books), (self.solo_maker, self.solo_books)):
            with self.subTest(school=books.code):
                response = self.fetch(user, books)
                self.assertEqual(response.status_code, 200, response.data)
                account = Account.objects.get(entity=books, code="5350")
                self.assertEqual(response.data["default_write_off_account"],
                                 {"id": account.pk, "code": "5350", "name": account.name})

    def test_a_mapping_the_school_changed_wins_over_the_default_code(self):
        other = Account.objects.create(
            entity=self.books, code="5399", name="Special bad debts", account_type="EXPENSE",
            is_postable=True)
        FinanceAccountMapping.objects.create(
            entity=self.books, key=AccountMappingKey.BAD_DEBT_EXPENSE, account=other)

        response = self.fetch(self.maker, self.books)

        self.assertEqual(response.data["default_write_off_account"]["code"], "5399")

    def test_it_is_null_when_the_entity_has_no_usable_bad_debt_account(self):
        Account.objects.filter(entity=self.books, code="5350").update(is_active=False)

        response = self.fetch(self.maker, self.books)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["default_write_off_account"])

    def test_a_reader_without_the_create_key_is_not_told(self):
        response = self.fetch(self.reader, self.books)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("default_write_off_account", response.data)

    def test_another_schools_entity_stays_closed_to_the_maker(self):
        self.assertEqual(self.fetch(self.maker, self.rival_books).status_code, 404)
