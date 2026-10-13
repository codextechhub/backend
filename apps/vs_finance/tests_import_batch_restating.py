"""An import batch's upload summary honours the bank statement balance switch.

A bank statement upload keeps the statement's opening and closing balances in
the batch's ``domain_context``. They are the registered
``finance.bankaccount.statement_balance`` seen from the import screens, so a
caller who may not read that field is served the summary without them.
"""
from __future__ import annotations

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_import_data.models import DatasetTypeChoices
from vs_rbac.tests.helpers import make_vision_user

from .tests import _Phase4FixtureMixin

HEADERS = "Transaction Date,Description,Reference,Money In,Money Out,Transaction ID,Balance\n"
ROWS = (
    "2026-01-02,Customer receipt,RCPT-1,500.00,,TX-1,1500.00\n"
    "2026-01-03,Bank fee,FEE-1,,50.00,TX-2,1450.00\n"
)
STATEMENT_BALANCE = "finance.bankaccount.statement_balance"
BALANCES = ("opening_balance", "opening_balance_major", "closing_balance", "closing_balance_major")


class ImportBatchDomainContextAccessTests(_Phase4FixtureMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = make_vision_user(email="batch-balances@test.com", super_admin=True)
        call_command(
            "seed_import", dataset_type=DatasetTypeChoices.BANK_STATEMENTS, verbosity=0)
        builder = cls()
        cls.entity, _, _ = builder.build_books()
        cls.bank = builder.make_bank(cls.entity)

    def setUp(self):
        super().setUp()
        self.client = TenantAPIClient(user=self.user)
        response = self.client.post(
            f"/v1/finance/bank-accounts/{self.bank.pk}/statement-imports/"
            f"?entity={self.entity.code}",
            {
                "file": SimpleUploadedFile(
                    "statement.csv", (HEADERS + ROWS).encode(), content_type="text/csv"),
                "statement_date": "2026-01-31", "period_label": "January 2026",
                "opening_balance": "1000.00", "closing_balance": "1450.00",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.batch_id = response.json()["data"]["id"]

    def context(self):
        response = self.client.get(f"/v1/import/batches/{self.batch_id}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["domain_context"]

    def test_a_reader_of_the_statement_balance_gets_the_upload_summary_whole(self):
        context = self.context()

        for name in BALANCES:
            self.assertIn(name, context)
        self.assertEqual(context["closing_balance"], 145000)

    def test_a_hidden_statement_balance_leaves_the_identity_and_drops_the_balances(self):
        with mock.patch(
            "vs_import_data.serializers.can_read",
            side_effect=lambda request, key: key != STATEMENT_BALANCE,
        ):
            context = self.context()

        for name in BALANCES:
            self.assertNotIn(name, context)
        self.assertEqual(context["bank_account_id"], self.bank.pk)
        self.assertEqual(context["period_label"], "January 2026")
        self.assertEqual(context["type"], "bank_statement")

    def test_another_switch_does_not_hide_the_balances(self):
        with mock.patch(
            "vs_import_data.serializers.can_read",
            side_effect=lambda request, key: key != "finance.bankaccount.book_balance",
        ):
            context = self.context()

        self.assertIn("closing_balance", context)
