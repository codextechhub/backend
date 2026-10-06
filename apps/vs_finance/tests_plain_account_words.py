"""The goods-received account is named for both its readers, and a school's own name is kept.

Corona's accountant reads the chart, the account mapping and the journals and
sees "GR/IR clearing (goods received, not yet billed)": the term she knows with
the words the bursar uses beside it. Mrs Okafor, the bursar, is only ever told
"goods received, not yet billed". A school still holding the name the chart
seeded ("GR/IR Clearing") has it renamed; Bright Star, which called the account
"Goods in transit", keeps that.
"""
from __future__ import annotations

import importlib

from django.apps import apps as live_apps

from .constants import GRIR_NAME, GRIR_PLAIN, AccountMappingKey
from .models import Account
from .tests_branch_scope import _FinanceBranchFixture

_migration = importlib.import_module("vs_finance.migrations.0067_plain_account_labels")


class GoodsReceivedAccountWordsTests(_FinanceBranchFixture):

    def test_the_seeded_chart_and_the_mapping_name_it_for_both_readers(self):
        self.assertEqual(Account.objects.get(entity=self.books, code="2150").name, GRIR_NAME)
        self.assertEqual(AccountMappingKey.GRIR_CLEARING.label, GRIR_NAME)
        self.assertIn(GRIR_PLAIN, GRIR_NAME)
        self.assertNotIn("GR/IR", GRIR_PLAIN)

    def test_only_an_untouched_seeded_name_is_renamed(self):
        Account.objects.filter(entity=self.books, code="2150").update(name="GR/IR Clearing")
        Account.objects.filter(entity=self.solo_books, code="2150").update(name="Goods in transit")

        _migration.rename_seeded(live_apps, None)

        self.assertEqual(Account.objects.get(entity=self.books, code="2150").name, GRIR_NAME)
        self.assertEqual(Account.objects.get(entity=self.solo_books, code="2150").name,
                         "Goods in transit")

        _migration.restore_seeded(live_apps, None)

        self.assertEqual(Account.objects.get(entity=self.books, code="2150").name, "GR/IR Clearing")
        self.assertEqual(Account.objects.get(entity=self.solo_books, code="2150").name,
                         "Goods in transit")
