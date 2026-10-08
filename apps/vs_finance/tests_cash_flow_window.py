"""The cash flow statement with nothing chosen covers this fiscal year, not the whole ledger.

Mrs Adeyemi, the bursar at Corona, opens Cash flow in March 2026 without picking
a month. The school's cash movements began in 2025, and a board pack that showed
2025 and 2026 together under one heading would overstate this year. With nothing
chosen the statement covers the fiscal year today falls in, the window the income
statement uses, opens with the cash held when that year began and still reconciles.
A month chosen explicitly opens with the cash held when it began, so March opens
with what February closed with.

Two shapes of school: Corona with three branches, where a branch reader's figures
narrow, and a single-branch school, where nothing changes but the dimension recedes.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, FiscalPeriod, FiscalYear, JournalEntry, JournalLine
from vs_finance.posting import post_journal
from vs_finance.reports import cash_flow_statement, default_fiscal_year
from vs_rbac.scoping import BranchScope

from .tests_branch_scope import _FinanceBranchFixture

MARCH_15 = datetime.date(2026, 3, 15)
TODAY = "vs_finance.reports.tenant_today"


class _WindowFixture(_FinanceBranchFixture):
    """Each school banked cash in 2025, and banked and paid some out in January to March 2026.

    Whole school: 50,000 held when 2026 began, then +70,000 in January, +12,000 in
    February and -1,000 in March. Ikeja: 40,000 held, then +40,000, +5,000, +1,000.
    Lekki: 10,000 held, then +30,000, +7,000, -2,000.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for entity, branches in (
            (cls.books, (cls.ikeja, cls.lekki)),
            (cls.solo_books, (cls.solo_main, cls.solo_main)),
        ):
            last_year = FiscalYear.objects.create(
                entity=entity, year=2025,
                start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 12, 31),
            )
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=last_year, period_no=1, name="Jan 2025",
                start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 1, 31),
            )
            this_year = FiscalYear.objects.get(entity=entity, year=2026)
            for number, name, last_day in ((2, "Feb 2026", 28), (3, "Mar 2026", 31)):
                FiscalPeriod.objects.create(
                    entity=entity, fiscal_year=this_year, period_no=number, name=name,
                    start_date=datetime.date(2026, number, 1),
                    end_date=datetime.date(2026, number, last_day),
                )
            first, second = branches
            cls.post(entity, first, datetime.date(2025, 1, 15), "1100", "4100", 40_000)
            cls.post(entity, second, datetime.date(2025, 1, 16), "1100", "4100", 10_000)
            cls.post(entity, first, datetime.date(2026, 1, 15), "1100", "4100", 60_000)
            cls.post(entity, second, datetime.date(2026, 1, 16), "1100", "4100", 30_000)
            cls.post(entity, first, datetime.date(2026, 1, 20), "5200", "1100", 20_000)
            cls.post(entity, first, datetime.date(2026, 2, 10), "1100", "4100", 5_000)
            cls.post(entity, second, datetime.date(2026, 2, 11), "1100", "4100", 7_000)
            cls.post(entity, first, datetime.date(2026, 3, 5), "1100", "4100", 1_000)
            cls.post(entity, second, datetime.date(2026, 3, 6), "5200", "1100", 2_000)
        cls.ikeja_scope = BranchScope(frozenset({cls.ikeja.id}), include_shared=False)
        cls.lekki_scope = BranchScope(frozenset({cls.lekki.id}), include_shared=False)

    @classmethod
    def post(cls, entity, branch, date, debit_code, credit_code, amount):
        period = FiscalPeriod.objects.get(
            entity=entity, start_date__lte=date, end_date__gte=date, is_closing=False)
        entry = JournalEntry.objects.create(
            entity=entity, branch=branch, date=date, period=period,
            narration="cash", source="MANUAL",
        )
        for number, (code, debit, credit) in enumerate(
            ((debit_code, amount, 0), (credit_code, 0, amount)), start=1,
        ):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=entity, code=code),
                debit=debit, credit=credit, line_no=number,
            )
        post_journal(entry)


class CashFlowDefaultWindowTests(_WindowFixture):
    """The service: which year, which opening, and that it still reconciles."""

    def test_the_default_window_leaves_last_years_movement_out_and_keeps_this_years(self):
        for books in (self.books, self.solo_books):
            with self.subTest(books=books.code), patch(TODAY, return_value=MARCH_15):
                cf = cash_flow_statement(books)

                self.assertEqual(cf.fiscal_year, 2026)
                self.assertEqual(cf.by_activity["operating"], 81_000)
                self.assertEqual(cf.net_change, 81_000)

    def test_opening_is_the_cash_held_when_the_year_began_and_the_statement_reconciles(self):
        for books in (self.books, self.solo_books):
            with self.subTest(books=books.code), patch(TODAY, return_value=MARCH_15):
                cf = cash_flow_statement(books)

                self.assertEqual(cf.opening_cash, 50_000)
                self.assertEqual(cf.closing_cash, 131_000)
                self.assertEqual(cf.opening_cash + cf.net_change, cf.closing_cash)
                self.assertTrue(cf.is_reconciled)

    def test_a_branch_reader_gets_their_own_branchs_year_and_it_reconciles(self):
        with patch(TODAY, return_value=MARCH_15):
            cf = cash_flow_statement(self.books, scope=self.ikeja_scope)

        self.assertEqual(cf.opening_cash, 40_000)
        self.assertEqual(cf.net_change, 46_000)
        self.assertEqual(cf.closing_cash, 86_000)
        self.assertTrue(cf.is_reconciled)

    def test_a_lekki_only_reader_is_narrowed_and_reconciles(self):
        with patch(TODAY, return_value=MARCH_15):
            cf = cash_flow_statement(self.books, scope=self.lekki_scope)
            march = cash_flow_statement(
                self.books, period=self.period(self.books, "Mar 2026"), scope=self.lekki_scope)

        self.assertEqual((cf.opening_cash, cf.net_change, cf.closing_cash), (10_000, 35_000, 45_000))
        self.assertTrue(cf.is_reconciled)
        self.assertEqual((march.opening_cash, march.net_change, march.closing_cash),
                         (47_000, -2_000, 45_000))
        self.assertTrue(march.is_reconciled)

    def period(self, books, name):
        return FiscalPeriod.objects.get(entity=books, name=name)

    def test_a_month_chosen_opens_with_the_cash_held_when_it_began(self):
        for books in (self.books, self.solo_books):
            with self.subTest(books=books.code):
                january = cash_flow_statement(books, period=self.period(books, "Jan 2026"))
                february = cash_flow_statement(books, period=self.period(books, "Feb 2026"))
                march = cash_flow_statement(books, period=self.period(books, "Mar 2026"))

                self.assertEqual((january.opening_cash, january.net_change), (50_000, 70_000))
                self.assertEqual(february.opening_cash, january.closing_cash)
                self.assertEqual(march.opening_cash, february.closing_cash)
                self.assertEqual((march.opening_cash, march.net_change, march.closing_cash),
                                 (132_000, -1_000, 131_000))

    def test_opening_plus_movement_is_closing_for_every_month_chosen(self):
        for books in (self.books, self.solo_books):
            for name in ("Jan 2025", "Jan 2026", "Feb 2026", "Mar 2026"):
                with self.subTest(books=books.code, month=name):
                    cf = cash_flow_statement(books, period=self.period(books, name))

                    self.assertIsNone(cf.fiscal_year)
                    self.assertEqual(cf.opening_cash + cf.net_change, cf.closing_cash)
                    self.assertTrue(cf.is_reconciled)

    def test_a_branch_reader_chooses_a_month_and_it_opens_with_their_own_cash(self):
        february = self.period(self.books, "Feb 2026")
        ikeja = cash_flow_statement(self.books, period=february, scope=self.ikeja_scope)
        lekki = cash_flow_statement(self.books, period=february, scope=self.lekki_scope)

        self.assertEqual((ikeja.opening_cash, ikeja.net_change, ikeja.closing_cash),
                         (80_000, 5_000, 85_000))
        self.assertEqual((lekki.opening_cash, lekki.net_change, lekki.closing_cash),
                         (40_000, 7_000, 47_000))

    def test_a_year_chosen_is_that_year(self):
        last_year = FiscalYear.objects.get(entity=self.books, year=2025)
        with patch(TODAY, return_value=MARCH_15):
            cf = cash_flow_statement(self.books, fiscal_year=last_year)

        self.assertEqual(cf.fiscal_year, 2025)
        self.assertEqual((cf.opening_cash, cf.net_change, cf.closing_cash), (0, 50_000, 50_000))

    def test_once_the_calendar_runs_out_the_last_year_opened_is_still_the_window(self):
        with patch(TODAY, return_value=datetime.date(2027, 6, 1)):
            cf = cash_flow_statement(self.books)

        self.assertEqual(cf.fiscal_year, 2026)
        self.assertEqual(cf.opening_cash + cf.net_change, cf.closing_cash)

    def test_the_default_year_is_the_one_the_income_statement_uses(self):
        from vs_finance.reports import income_statement_compare

        with patch(TODAY, return_value=MARCH_15):
            self.assertEqual(default_fiscal_year(self.books).year, 2026)
            self.assertEqual(
                income_statement_compare(self.books).fiscal_year,
                cash_flow_statement(self.books).fiscal_year,
            )

    def test_with_no_fiscal_year_at_all_the_window_is_the_whole_ledger(self):
        from vs_finance.models import LedgerEntity
        from vs_finance.seed import seed_chart_of_accounts

        bare = LedgerEntity.objects.create(
            name="Bare Books", code="BARE", kind=LedgerEntity.Kind.TENANT, tenant=self.tenant,
        )
        seed_chart_of_accounts(bare)

        cf = cash_flow_statement(bare)

        self.assertIsNone(default_fiscal_year(bare))
        self.assertIsNone(cf.fiscal_year)
        self.assertTrue(cf.is_reconciled)


class CashFlowDefaultWindowEndpointTests(_WindowFixture):
    """The screen and the file name the window the way the income statement does."""

    def client_holding(self, email, *keys, tenant=None, branch=None):
        tenant = tenant or self.tenant
        user = self.grant(
            self.user_for(tenant, email), *keys,
            tenant=tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def get(self, client, books, path, suffix=""):
        return client.get(f"/v1/finance/reports/{path}/?entity={books.code}{suffix}")

    def body(self, response):
        raw = b"".join(response.streaming_content) if response.streaming else response.content
        return raw.decode()

    def test_the_response_names_the_year_and_its_figures_cover_it(self):
        school = self.client_holding("cf-hq@corona.test", "finance.report.view")
        with patch(TODAY, return_value=MARCH_15):
            response = self.get(school, self.books, "cash-flow")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["fiscal_year"], 2026)
        self.assertIsNone(data["period"])
        self.assertIsNone(data["period_label"])
        self.assertEqual(data["opening_cash"]["kobo"], 50_000)
        self.assertEqual(data["net_change"]["kobo"], 81_000)
        self.assertEqual(data["closing_cash"]["kobo"], 131_000)
        self.assertTrue(data["is_reconciled"])

    def test_the_file_and_the_income_statement_head_the_same_year(self):
        school = self.client_holding("cf-file@corona.test", "finance.report.view")
        with patch(TODAY, return_value=MARCH_15):
            cash = self.body(self.get(school, self.books, "cash-flow", "&export=csv"))
            income = self.body(self.get(school, self.books, "income-statement", "&export=csv"))
            income_data = self.get(school, self.books, "income-statement").data["data"]
            cash_data = self.get(school, self.books, "cash-flow").data["data"]

        self.assertIn("FY2026", cash)
        self.assertNotIn("Year to date", cash)
        self.assertIn("FY2026", income)
        self.assertEqual(cash_data["fiscal_year"], income_data["fiscal_year"])

    def test_a_single_branch_school_reads_the_same_way(self):
        reader = self.client_holding(
            "cf-solo@solo.test", "finance.report.view", tenant=self.solo_tenant)
        with patch(TODAY, return_value=MARCH_15):
            data = self.get(reader, self.solo_books, "cash-flow").data["data"]
            text = self.body(self.get(reader, self.solo_books, "cash-flow", "&export=csv"))

        self.assertEqual(data["fiscal_year"], 2026)
        self.assertEqual(data["opening_cash"]["kobo"], 50_000)
        self.assertEqual(data["closing_cash"]["kobo"], 131_000)
        self.assertIn("FY2026", text)

    def test_a_single_branch_reader_can_open_the_statutory_pack(self):
        reader = self.client_holding(
            "pack-solo@solo.test", "finance.report.view", tenant=self.solo_tenant,
        )
        with patch(TODAY, return_value=MARCH_15):
            response = self.get(reader, self.solo_books, "statutory-pack")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["headings"]["cash_flow"], "Fiscal year 2026")

    def test_a_branch_reader_is_narrowed_and_still_reconciles(self):
        ikeja = self.client_holding(
            "cf-ikeja@corona.test", "finance.report.view", branch=self.ikeja)
        with patch(TODAY, return_value=MARCH_15):
            data = self.get(ikeja, self.books, "cash-flow").data["data"]

        self.assertTrue(data["narrowed"])
        self.assertEqual(data["opening_cash"]["kobo"], 40_000)
        self.assertEqual(data["closing_cash"]["kobo"], 86_000)
        self.assertTrue(data["is_reconciled"])

    def test_a_month_chosen_keeps_its_own_heading_and_opens_with_the_cash_then_held(self):
        school = self.client_holding("cf-month@corona.test", "finance.report.view")
        with patch(TODAY, return_value=MARCH_15):
            response = self.get(school, self.books, "cash-flow", "&period=3")
            text = self.body(self.get(
                school, self.books, "cash-flow", "&period=3&export=csv"))

        data = response.data["data"]
        self.assertEqual(data["period_label"], "March 2026")
        self.assertIsNone(data["fiscal_year"])
        self.assertEqual(data["opening_cash"]["kobo"], 132_000)
        self.assertEqual(data["net_change"]["kobo"], -1_000)
        self.assertEqual(data["closing_cash"]["kobo"], 131_000)
        self.assertTrue(data["is_reconciled"])
        self.assertIn("March 2026", text)
        self.assertNotIn("FY2026", text)

    def test_a_lekki_only_reader_chooses_a_month_and_is_narrowed(self):
        lekki = self.client_holding(
            "cf-lekki@corona.test", "finance.report.view", branch=self.lekki)
        with patch(TODAY, return_value=MARCH_15):
            data = self.get(lekki, self.books, "cash-flow", "&period=3").data["data"]

        self.assertTrue(data["narrowed"])
        self.assertEqual(data["opening_cash"]["kobo"], 47_000)
        self.assertEqual(data["closing_cash"]["kobo"], 45_000)
        self.assertTrue(data["is_reconciled"])

    def test_a_reader_without_the_report_permission_is_refused(self):
        nobody = self.client_holding("cf-none@corona.test", "finance.journal.view")

        self.assertEqual(self.get(nobody, self.books, "cash-flow").status_code, 403)
