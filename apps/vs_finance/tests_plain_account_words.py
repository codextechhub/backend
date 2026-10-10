"""The goods-received account has a concise name and keeps school wording."""
from __future__ import annotations

import importlib

from django.apps import apps as live_apps

from .constants import GRIR_NAME, GRIR_PLAIN, AccountMappingKey
from .models import Account
from .tests_branch_scope import _FinanceBranchFixture

_migration = importlib.import_module("vs_finance.migrations.0070_simple_account_names")


class GoodsReceivedAccountWordsTests(_FinanceBranchFixture):

    def test_the_seeded_chart_and_mapping_use_the_account_name_only(self):
        self.assertEqual(Account.objects.get(entity=self.books, code="2150").name, GRIR_NAME)
        self.assertEqual(AccountMappingKey.GRIR_CLEARING.label, GRIR_NAME)
        self.assertNotIn(GRIR_PLAIN, GRIR_NAME)
        self.assertNotIn("GR/IR", GRIR_PLAIN)

    def test_only_an_untouched_seeded_name_is_renamed(self):
        Account.objects.filter(entity=self.books, code="2150").update(
            name="GR/IR clearing (goods received, not yet billed)"
        )
        Account.objects.filter(entity=self.solo_books, code="2150").update(name="Goods in transit")

        _migration.shorten_seeded_names(live_apps, None)

        self.assertEqual(Account.objects.get(entity=self.books, code="2150").name, GRIR_NAME)
        self.assertEqual(Account.objects.get(entity=self.solo_books, code="2150").name,
                         "Goods in transit")

        _migration.restore_paired_names(live_apps, None)

        self.assertEqual(
            Account.objects.get(entity=self.books, code="2150").name,
            "GR/IR clearing (goods received, not yet billed)",
        )
        self.assertEqual(Account.objects.get(entity=self.solo_books, code="2150").name,
                         "Goods in transit")
