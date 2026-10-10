"""Account names stay concise while refusals explain what users need to fix."""
from __future__ import annotations

import datetime
import importlib
from types import SimpleNamespace

from django.apps import apps as live_apps
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError

from .constants import (
    AP_NAME,
    AR_NAME,
    GATEWAY_NAME,
    WHT_NAME,
    AccountMappingKey,
    PayeMethod,
)
from .exceptions import MissingAccountError, NoReceivableAccountError
from .models import Account, FiscalPeriod
from .tests_branch_scope import _FinanceBranchFixture

_migration = importlib.import_module("vs_finance.migrations.0070_simple_account_names")

#: Words that never reach a bursar on their own.
BURSAR_JARGON = ("control", "sub-ledger", "subledger", "legacy", "cutover", "entity", "postable")


class SimpleAccountNamesTests(_FinanceBranchFixture):

    SEEDED = {"1125": GATEWAY_NAME, "1200": AR_NAME, "2100": AP_NAME, "2300": WHT_NAME}

    def names(self, books):
        return dict(
            Account.objects.filter(entity=books, code__in=self.SEEDED).values_list("code", "name")
        )

    def test_the_seeded_chart_and_the_mapping_use_short_account_names(self):
        self.assertEqual(self.names(self.books), self.SEEDED)
        self.assertEqual(AccountMappingKey.GATEWAY_CLEARING.label, GATEWAY_NAME)
        self.assertEqual(AccountMappingKey.ACCOUNTS_RECEIVABLE.label, AR_NAME)
        self.assertEqual(AccountMappingKey.ACCOUNTS_PAYABLE.label, AP_NAME)
        self.assertEqual(AccountMappingKey.WHT_PAYABLE.label, "WHT payable")

    def test_only_an_untouched_seeded_name_is_renamed(self):
        seeded = {
            "1125": "Gateway clearing (online payments not yet in the bank)",
            "1200": "Accounts receivable (what customers owe)",
            "2100": "Accounts payable (what is owed to suppliers)",
            "2300": "WHT payable (withholding tax)",
        }
        for code, name in seeded.items():
            Account.objects.filter(entity=self.books, code=code).update(name=name)
            Account.objects.filter(entity=self.solo_books, code=code).update(name=name)
        Account.objects.filter(entity=self.solo_books, code="2300").update(
            name="Withholding tax payable")

        _migration.shorten_seeded_names(live_apps, None)

        self.assertEqual(self.names(self.books), self.SEEDED)
        self.assertEqual(self.names(self.solo_books)["2300"], "Withholding tax payable")
        self.assertEqual(self.names(self.solo_books)["1200"], AR_NAME)

        _migration.restore_paired_names(live_apps, None)

        self.assertEqual(self.names(self.books), seeded)
        self.assertEqual(self.names(self.solo_books)["2300"], "Withholding tax payable")

    def test_a_gateway_account_placed_at_another_code_is_renamed_too(self):
        Account.objects.filter(entity=self.books, code="1125").update(
            code="1126", name="Gateway clearing (online payments not yet in the bank)"
        )

        _migration.shorten_seeded_names(live_apps, None)

        self.assertEqual(Account.objects.get(entity=self.books, code="1126").name, GATEWAY_NAME)


class RefusalsSpeakToTheirReaderTests(SimpleTestCase):

    def assert_plain(self, text):
        for word in BURSAR_JARGON:
            self.assertNotIn(word, text.lower())

    def test_a_bursar_gets_plain_words_and_an_accountant_gets_the_account_name(self):
        from vs_procurement.exceptions import NoPayableAccountError

        customer = SimpleNamespace(code="C001", pk=1)
        vendor = SimpleNamespace(code="V001", pk=1)

        self.assertEqual(
            str(NoReceivableAccountError(customer)),
            "Customer C001 has no account chosen for what they owe, so nothing can be posted "
            "for them yet. Choose one on the customer's record.",
        )
        self.assert_plain(str(NoPayableAccountError(vendor)))
        self.assertIn("accounts receivable account",
                      str(NoReceivableAccountError(customer, accountant=True)))
        self.assertIn("accounts payable account",
                      str(NoPayableAccountError(vendor, accountant=True)))

    def test_a_missing_account_is_named_by_code_and_role(self):
        from vs_procurement.exceptions import MissingControlAccountError

        message = str(MissingAccountError("2300", label=WHT_NAME))

        self.assertTrue(message.startswith(
            "Account 2300, WHT payable, is missing from the chart of accounts"
        ), message)
        self.assertNotIn("entity", message)
        self.assertNotIn("postable", message)
        self.assertNotIn("entity", str(MissingControlAccountError("2150", label="x")))


class SettingChoicesHaveOneLabelTests(SimpleTestCase):

    def test_the_dropdown_the_history_and_the_refusal_share_the_paye_label(self):
        from .models import FinancePayrollSettings
        from .payroll_settings import _validated_values, serialize_finance_payroll_settings
        from .settings_history import PAYROLL_SETTING_FIELDS, describe_value

        supplied = "Taken from the salary structure or roster"
        data = serialize_finance_payroll_settings(
            FinancePayrollSettings(paye_method=PayeMethod.SUPPLIED))

        self.assertEqual(data["paye_method_label"], supplied)
        self.assertEqual(data["paye_method_options"], [
            {"value": "COMPUTED", "label": "Computed from the national tax table"},
            {"value": "SUPPLIED", "label": supplied},
        ])
        self.assertEqual(describe_value(PAYROLL_SETTING_FIELDS["paye_method"], "SUPPLIED"),
                         supplied)
        with self.assertRaises(ValidationError) as refused:
            _validated_values({"paye_method": "SCHOOL"})
        self.assertIn(f'"{supplied}"', str(refused.exception.detail["paye_method"]))
        self.assertNotIn("SUPPLIED", str(refused.exception.detail["paye_method"]))

    def test_every_choice_setting_offers_its_options(self):
        from .banking_settings import serialize_finance_banking_settings
        from .calendar_settings import serialize_finance_calendar_settings
        from .models import FinanceBankingSettings, FinanceCalendarSettings

        banking = serialize_finance_banking_settings(FinanceBankingSettings())
        calendar = serialize_finance_calendar_settings(FinanceCalendarSettings())

        self.assertEqual(
            [row["label"] for row in banking["default_receipt_allocation_strategy_options"]],
            list(FinanceBankingSettings.ReceiptAllocationStrategy.labels),
        )
        self.assertEqual(
            [row["label"] for row in calendar["next_year_mode_options"]],
            list(FinanceCalendarSettings.NextYearMode.labels),
        )


class PeriodsAreOfferedInWordsTests(_FinanceBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        year = FiscalPeriod.objects.get(entity=cls.books, period_no=1).fiscal_year
        for number in range(2, 13):
            start = datetime.date(2026, number, 1)
            end = (datetime.date(2026, number + 1, 1) if number < 12
                   else datetime.date(2027, 1, 1)) - datetime.timedelta(days=1)
            FiscalPeriod.objects.create(
                entity=cls.books, fiscal_year=year, period_no=number,
                name=f"2026-{number:02d}", start_date=start, end_date=end,
            )

    def test_a_period_read_carries_its_month_in_words(self):
        from .serializers import FiscalPeriodSerializer

        february = FiscalPeriod.objects.get(entity=self.books, period_no=2)
        data = FiscalPeriodSerializer(february).data

        self.assertEqual((data["name"], data["label"]), ("2026-02", "February 2026"))

    def test_a_list_of_periods_reads_the_school_once(self):
        from .serializers import FiscalPeriodSerializer

        periods = (FiscalPeriod.objects.filter(entity=self.books)
                   .select_related("fiscal_year").order_by("period_no"))
        first, rows = list(periods[:1]), list(periods)
        with CaptureQueriesContext(connection) as one:
            FiscalPeriodSerializer(first, many=True).data
        with CaptureQueriesContext(connection) as twelve:
            data = FiscalPeriodSerializer(rows, many=True).data

        self.assertEqual(len(twelve), len(one))
        self.assertEqual(data[8]["label"], "September 2026")

    def test_the_posting_window_offers_months_in_words(self):
        from .posting import posting_window

        window = posting_window(self.books, today=datetime.date(2026, 9, 15))

        self.assertEqual(window["default_period"]["label"], "September 2026")
        self.assertIn("February 2026", [row["label"] for row in window["open"]])

    def test_a_deferred_income_release_names_its_month(self):
        from .views_accruals import _release_row

        september = FiscalPeriod.objects.get(entity=self.books, period_no=9)
        release = SimpleNamespace(
            pk=1, branch_id=None, branch=None, reversed_at=None, amount=100,
            journal=SimpleNamespace(date=datetime.date(2026, 9, 30), document_number="JE-1"),
            journal_id=1, period_id=september.pk, period_name=september.name,
            period_start=september.start_date, period_end=september.end_date,
            period_status="OPEN", branch_period_status="OPEN",
        )

        row = _release_row(release, (), self.tenant)

        self.assertEqual((row["period_name"], row["period_label"]), ("2026-09", "September 2026"))
