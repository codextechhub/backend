"""A month, and how a payroll figure was reached, are read in words beside their code.

Mrs Okafor, the bursar at Corona, opens the journals list in September and reads
"September 2026", never the stored "2026-09". Every screen that names a period
asks the server for the same words (:func:`vs_finance.wording.period_label`), and
a list of journals does not cost a query per row to get them. The same holds for
the way a payroll line's PAYE was reached, which reads in the plain words the
payroll settings use for the same two ways.
"""
from __future__ import annotations

import datetime

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient

from .constants import PayeMethod, PayeSource
from .models import FiscalPeriod, JournalEntry, PayrollLine, PayrollRun
from .tests_branch_scope import _FinanceBranchFixture

#: A request for a list of journals costs this many queries however long the list is.
JOURNAL_LIST_QUERY_BOUND = 18


class _MonthsFixture(_FinanceBranchFixture):
    """Corona's books with February and March beside January, named as the calendar stores them."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        january = FiscalPeriod.objects.get(entity=cls.books, period_no=1)
        cls.january = january
        cls.february = cls.month(cls.books, january.fiscal_year, 2, datetime.date(2026, 2, 28))
        cls.march = cls.month(cls.books, january.fiscal_year, 3, datetime.date(2026, 3, 31))

    @classmethod
    def month(cls, books, year, number, end):
        return FiscalPeriod.objects.create(
            entity=books, fiscal_year=year, period_no=number, name=f"2026-{number:02d}",
            start_date=datetime.date(2026, number, 1), end_date=end,
        )

    @classmethod
    def journal(cls, period, day=10):
        return JournalEntry.objects.create(
            entity=cls.books, branch=cls.ikeja, period=period,
            date=period.start_date.replace(day=day),
        )

    @classmethod
    def user_holding(cls, email, *keys):
        return cls.grant(
            cls.user_for(cls.tenant, email), *keys,
            tenant=cls.tenant, role_key=f"role-{email}",
        )

    def url(self, path, suffix=""):
        return f"/v1/finance/{path}?entity={self.books.code}{suffix}"


class JournalMonthTests(_MonthsFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.february_journal = cls.journal(cls.february)
        cls.march_journal = cls.journal(cls.march)
        cls.undated = JournalEntry.objects.create(
            entity=cls.books, branch=cls.ikeja, date=datetime.date(2026, 2, 12),
        )
        cls.reader_user = cls.user_holding("journals@corona.test", "finance.journal.view")

    def setUp(self):
        super().setUp()
        self.reader = TenantAPIClient(user=self.reader_user)


    def test_the_list_names_each_month_beside_its_stored_name(self):
        rows = {
            row["id"]: row
            for row in self.reader.get(self.url("journals/")).data["data"]
        }

        february = rows[self.february_journal.pk]
        self.assertEqual((february["period"], february["period_label"]), ("2026-02", "February 2026"))
        self.assertEqual(rows[self.march_journal.pk]["period_label"], "March 2026")
        self.assertEqual(
            (rows[self.undated.pk]["period"], rows[self.undated.pk]["period_label"]), (None, None),
        )

    def test_the_detail_names_the_month_too(self):
        response = self.reader.get(self.url(f"journals/{self.february_journal.pk}/"))

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual((data["period"], data["period_label"]), ("2026-02", "February 2026"))

    def test_a_longer_list_costs_no_more_queries(self):
        self.reader.get(self.url("journals/"))
        with CaptureQueriesContext(connection) as short:
            self.reader.get(self.url("journals/"))
        for _ in range(4):
            self.journal(self.february)
            self.journal(self.march)
        with CaptureQueriesContext(connection) as long:
            response = self.reader.get(self.url("journals/"))

        self.assertEqual(len(response.data["data"]), 11)
        self.assertEqual(len(long), len(short))
        self.assertLessEqual(len(long), JOURNAL_LIST_QUERY_BOUND)

    def test_a_set_of_journals_reads_the_school_once_for_their_months(self):
        """What a year close returns: several journals through one serializer."""
        from .serializers import JournalEntryDetailSerializer

        journals = list(
            JournalEntry.objects.filter(entity=self.books).select_related("period")
        )
        with CaptureQueriesContext(connection) as books_read:
            JournalEntryDetailSerializer(journals, many=True).data
        tenant_reads = [
            q for q in books_read.captured_queries
            if 'FROM "vs_finance_ledgerentity"' in q["sql"] and "vs_tenants_tenant" in q["sql"]
        ]

        self.assertEqual(len(tenant_reads), 1)
        data = JournalEntryDetailSerializer(journals, many=True).data
        self.assertEqual(
            {row["period_label"] for row in data}, {"February 2026", "March 2026", None},
        )


class DashboardMonthTests(_MonthsFixture):

    def test_the_overview_and_its_close_progress_name_the_month(self):
        from .dashboard import finance_dashboard

        data = finance_dashboard(self.books, period=self.february)

        self.assertEqual((data["period"], data["period_label"]), ("2026-02", "February 2026"))
        progress = data["close_progress"]
        self.assertEqual(
            (progress["period"], progress["period_label"]), ("2026-02", "February 2026"),
        )

    def test_a_one_branch_school_reads_the_same_words(self):
        from .dashboard import finance_dashboard

        data = finance_dashboard(self.solo_books)

        self.assertEqual(data["period_label"], "January 2026")
        self.assertEqual(data["close_progress"]["period_label"], "January 2026")


class ReportMonthTests(_MonthsFixture):

    #: Every report that names the period it covers, with what each needs to be asked.
    REPORTS = (
        "trial-balance", "income-statement", "cash-flow", "changes-in-equity",
        "analytics-slice", "statutory-pack",
    )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.accountant_user = cls.user_holding("reports@corona.test", "finance.report.view")

    def setUp(self):
        super().setUp()
        self.accountant = TenantAPIClient(user=self.accountant_user)


    def read(self, report, suffix=""):
        extra = "&axis=cost_center" if report == "analytics-slice" else ""
        response = self.accountant.get(self.url(f"reports/{report}/", extra + suffix))
        self.assertEqual(response.status_code, 200, (report, response.data))
        return response.data["data"]

    def test_each_report_names_its_month_beside_the_stored_name(self):
        for report in self.REPORTS:
            with self.subTest(report=report):
                data = self.read(report, "&period=2")

                self.assertEqual(data["period"], "2026-02")
                self.assertEqual(data["period_label"], "February 2026")

    def test_a_report_over_every_period_has_no_label(self):
        for report in self.REPORTS:
            with self.subTest(report=report):
                data = self.read(report)

                self.assertIsNone(data["period"])
                self.assertIsNone(data["period_label"])

    def test_the_downloaded_file_says_the_month_in_words(self):
        for report, filename in (
            ("trial-balance", "trial_balance"), ("cash-flow", "cash_flow"),
            ("changes-in-equity", "changes_in_equity"),
            ("analytics-slice", "analytics_slice"),
        ):
            with self.subTest(report=report):
                extra = "&axis=cost_center" if report == "analytics-slice" else ""
                response = self.accountant.get(
                    self.url(f"reports/{report}/", f"{extra}&period=2&export=csv"))

                text = response.content.decode()
                self.assertIn("February 2026", text)
                self.assertNotIn("2026-02", text)


class PayrollLineWordsTests(_MonthsFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.payroll_run = PayrollRun.objects.create(
            entity=cls.books, branch=cls.ikeja, pay_date=datetime.date(2026, 2, 27),
        )
        for number, source in enumerate(PayeSource.values, start=1):
            PayrollLine.objects.create(
                run=cls.payroll_run, line_no=number, employee_name=f"Staff {number}",
                gross_amount=100_000, paye_source=source, branch=cls.ikeja,
            )
        cls.payroll_user = cls.user_holding("payroll@corona.test", "finance.payrollrun.view")

    def setUp(self):
        super().setUp()
        self.payroll_reader = TenantAPIClient(user=self.payroll_user)


    def test_a_line_says_how_its_paye_was_reached(self):
        response = self.payroll_reader.get(self.url(f"payroll-runs/{self.payroll_run.pk}/"))

        self.assertEqual(response.status_code, 200, response.data)
        said = {line["paye_source"]: line["paye_source_label"] for line in response.data["data"]["lines"]}
        self.assertEqual(said, {value: PayeSource(value).label for value in PayeSource.values})

    def test_the_list_says_it_too(self):
        response = self.payroll_reader.get(self.url("payroll-runs/"))

        lines = response.data["data"][0]["lines"]
        self.assertTrue(all(line["paye_source_label"] for line in lines))


class PayeWordsAgreeTests(SimpleTestCase):

    def test_a_line_and_the_setting_use_one_label_for_the_same_way(self):
        self.assertEqual(PayeSource.COMPUTED.label, PayeMethod.COMPUTED.label)
        self.assertEqual(PayeSource.SUPPLIED.label, PayeMethod.SUPPLIED.label)
        self.assertEqual(PayeSource.COMPUTED.label, "Computed from the national tax table")
        self.assertEqual(PayeSource.SUPPLIED.label, "Taken from the salary structure or roster")
