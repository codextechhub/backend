"""The Cash, spend & compliance tab.

The same Corona school as the overview tests, read as at 31 January 2026: Ikeja's
Tunde pays 60,000 and Lekki's Aisha 40,000 into the bank on 15 January. On top of
that, Ikeja pays a 30,000 electricity bill from the bank on 20 January, charged
to the Facilities cost centre, and Lekki pays a 10,000 cleaning bill the same
day with no cost centre.

Cash and the school's bank, payroll and tax blocks are for readers who see the
whole school. Spending, budgets and expense claims answer under a branch
reader's branches, and a school-wide plan's figures are withheld from them.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.dashboard import EVERY_BLOCK
from vs_finance.dashboard_spend import _account_sets, runway, spend_view
from vs_finance.models import Account, CostCenter, FiscalPeriod, JournalEntry, JournalLine

from .tests_dashboard_overview import _OverviewFixture


class _SpendFixture(_OverviewFixture):
    def setUp(self):
        super().setUp()
        self.facilities = CostCenter.objects.create(entity=self.books, code="FAC", name="Facilities")
        self.bill(self.ikeja, 30_000, cost_center=self.facilities)
        self.bill(self.lekki, 10_000)

    def acct(self, code):
        return Account.objects.get(entity=self.books, code=code)

    def journal(self, branch, lines, date=datetime.date(2026, 1, 20)):
        from vs_finance.posting import post_journal

        entry = JournalEntry.objects.create(
            entity=self.books, branch=branch, date=date, source="BANK",
            period=FiscalPeriod.objects.get(entity=self.books, period_no=1),
        )
        for n, (code, debit, credit, cc) in enumerate(lines, start=1):
            JournalLine.objects.create(entry=entry, line_no=n, account=self.acct(code),
                                       debit=debit, credit=credit, cost_center=cc)
        post_journal(entry)
        return entry

    def bill(self, branch, amount, cost_center=None):
        return self.journal(branch, [("5300", amount, 0, cost_center), ("1100", 0, amount, None)])

    def view(self, window="month", reader=None):
        return spend_view(self.books, reader=reader or EVERY_BLOCK, window=window, period=self.period)


class CashMovementTests(_SpendFixture):
    def test_the_month_runs_from_the_opening_balance_to_today_by_kind(self):
        cash = self.view()["cash_movement"]
        steps = {s["key"]: s["amount"]["kobo"] for s in cash["steps"]}
        self.assertEqual(cash["opening"]["kobo"], 0)
        self.assertEqual(steps, {"receipts": 100_000, "spending": -40_000})
        self.assertEqual(cash["closing"]["kobo"], 60_000)

    def test_money_moved_between_the_schools_own_accounts_is_left_out(self):
        from vs_finance.models import BankAccount

        operations = Account.objects.create(entity=self.books, code="1101", name="Operations",
                                            account_type="ASSET", parent=self.acct("1000"), is_postable=True)
        BankAccount.objects.create(entity=self.books, name="Operations", bank_name="Zenith",
                                   account_number="0123456789", gl_account=operations)
        self.journal(None, [("1101", 20_000, 0, None), ("1100", 0, 20_000, None)])
        steps = {s["key"] for s in self.view()["cash_movement"]["steps"]}
        self.assertEqual(steps, {"receipts", "spending"})

    def test_capital_put_in_is_its_own_step_not_income(self):
        self.journal(None, [("1100", 500_000, 0, None), ("3100", 0, 500_000, None)])
        steps = {s["key"]: s["amount"]["kobo"] for s in self.view()["cash_movement"]["steps"]}
        self.assertEqual(steps["equity"], 500_000)
        self.assertNotIn("other_income", steps)


class RunwayTests(_SpendFixture):
    def test_runway_averages_the_outflow_over_the_history_the_books_have(self):
        r = self.view()["runway"]
        # First cash on 15 January, read on the 31st: 17 days, 40,000 out.
        self.assertEqual(r["based_on_days"], 17)
        self.assertEqual(r["monthly_outflow"]["kobo"], 40_000 * 30 // 17)
        self.assertEqual(r["cash"]["kobo"], 60_000)

    def test_under_two_weeks_of_history_gives_no_runway(self):
        r = runway(self.books, datetime.date(2026, 1, 20), _account_sets(self.books), 60_000)
        self.assertIsNone(r["months"])


class SpendingTests(_SpendFixture):
    def test_spending_groups_by_cost_centre_with_untagged_spending_shown(self):
        spending = self.view()["spending"]
        self.assertEqual(spending["basis"], "cost_centre")
        self.assertEqual([(i["name"], i["amount"]["kobo"]) for i in spending["items"]],
                         [("Facilities", 30_000), ("Not tagged", 10_000)])

    def test_books_without_cost_centres_group_by_expense_account(self):
        JournalLine.objects.filter(cost_center=self.facilities).update(cost_center=None)
        spending = self.view()["spending"]
        self.assertEqual(spending["basis"], "account")
        self.assertEqual(spending["items"][0]["name"], "General & Administrative")

    def test_a_branch_reader_sees_only_their_branchs_spending(self):
        ikeja = self.reader("finance.report.view", branch=self.ikeja)
        d = self.view(reader=ikeja)
        self.assertEqual(d["spend"]["amount"]["kobo"], 30_000)
        self.assertIsNone(d["cash_movement"])
        self.assertIsNone(d["runway"])


class ClaimTests(_SpendFixture):
    def claim(self, branch, amount, state):
        from vs_finance.expenses import post_expense_claim, price_expense_claim, settle_expense_claim
        from vs_finance.models import BankAccount, ExpenseClaim, ExpenseClaimLine

        claim = ExpenseClaim.objects.create(entity=self.books, branch=branch, claimant_name="Mrs Eze",
                                            claim_date=datetime.date(2026, 1, 10), title="Teaching aids")
        ExpenseClaimLine.objects.create(claim=claim, line_no=1, expense_account=self.acct("5300"),
                                        quantity=1, unit_price=amount)
        price_expense_claim(claim)
        if state == "waiting":
            claim.status = "PENDING_APPROVAL"
            claim.save(update_fields=["status"])
            return claim
        post_expense_claim(claim)
        if state == "paid":
            bank, _ = BankAccount.objects.get_or_create(
                entity=self.books, name="Main", defaults={"bank_name": "GTB", "account_number": "0987654321",
                                                          "gl_account": self.acct("1100")})
            settle_expense_claim(claim, bank_account=bank, pay_date=datetime.date(2026, 1, 25))
        return claim

    def test_claims_are_counted_at_each_stage(self):
        self.claim(self.ikeja, 5_000, "waiting")
        self.claim(self.ikeja, 7_000, "owed")
        self.claim(self.ikeja, 9_000, "paid")
        claims = self.view()["claims"]
        self.assertEqual((claims["submitted"]["count"], claims["submitted"]["amount"]["kobo"]), (1, 5_000))
        self.assertEqual((claims["approved"]["count"], claims["approved"]["amount"]["kobo"]), (1, 7_000))
        self.assertEqual((claims["paid"]["count"], claims["paid"]["amount"]["kobo"]), (1, 9_000))
        self.assertEqual(claims["oldest"][0]["days"], 21)

    def test_a_branch_reader_does_not_see_another_branchs_claims(self):
        self.claim(self.lekki, 5_000, "waiting")
        ikeja = self.reader("finance.expenseclaim.view", branch=self.ikeja)
        self.assertEqual(self.view(reader=ikeja)["claims"]["submitted"]["count"], 0)


class BudgetAndAccessTests(_SpendFixture):
    def plan(self, branch=None):
        from vs_finance.models import Budget, BudgetLine

        budget = Budget.objects.create(entity=self.books, branch=branch, fiscal_year=self.period.fiscal_year,
                                       name=f"Plan {getattr(branch, 'name', 'school')}")
        BudgetLine.objects.create(budget=budget, account=self.acct("5300"), period_no=1, amount=80_000)
        return budget

    def test_a_plan_shows_how_much_of_its_spending_is_used(self):
        self.plan()
        item = self.view()["budgets"]["items"][0]
        self.assertEqual((item["plan"]["kobo"], item["used"]["kobo"], item["pct"]), (80_000, 40_000, 50.0))

    def test_a_branch_reader_sees_their_plan_but_not_the_schools_figures(self):
        self.plan()
        self.plan(self.ikeja)
        ikeja = self.reader("finance.budget.view", branch=self.ikeja)
        items = {i["branch"]: i for i in self.view(reader=ikeja)["budgets"]["items"]}
        self.assertIsNone(items[None]["used"])
        self.assertEqual(items[self.ikeja.name]["used"]["kobo"], 30_000)

    def test_the_schools_money_is_for_whole_school_readers_only(self):
        ikeja = self.reader("finance.report.view", "finance.payrollrun.view", "finance.tax.view",
                            "finance.bankaccount.view", branch=self.ikeja)
        d = self.view(reader=ikeja)
        for block in ("payroll", "tax_owed", "tax_calendar", "reconciliation", "unmatched", "cash_movement"):
            self.assertIsNone(d[block], block)

    def test_blocks_follow_their_keys(self):
        d = self.view(reader=self.reader("finance.invoice.view"))
        for block in ("spend", "spending", "budgets", "claims", "petty_cash", "assets", "payroll", "tax_owed"):
            self.assertIsNone(d[block], block)


class TaxCalendarTests(_SpendFixture):
    def test_a_return_with_nothing_owed_still_shows_as_a_nil_return(self):
        from vs_finance.models import TaxFiling, TaxObligation

        vat = TaxObligation.objects.get(entity=self.books, code="VAT")
        TaxFiling.objects.create(entity=self.books, obligation=vat, period_start=datetime.date(2026, 1, 1),
                                 period_end=datetime.date(2026, 1, 31), due_date=datetime.date(2026, 2, 21))
        row = self.view()["tax_calendar"][0]
        self.assertEqual((row["name"], row["state"], row["days"]), ("Value Added Tax", "nil", 21))


class SpendEndpointTests(_SpendFixture):
    def test_the_endpoint_opens_to_a_finance_reader(self):
        user = self.grant(self.user_for(self.tenant, "spend-tab@corona.test"), "finance.report.view",
                          tenant=self.tenant, role_key="role-spend-tab")
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/reports/dashboard/spend/?entity={self.books.code}&window=month&period=1",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.json()["data"]["spend"]["amount"]["kobo"], 40_000)

    def test_a_reader_with_no_finance_key_is_refused(self):
        user = self.grant(self.user_for(self.tenant, "no-fin-spend@corona.test"), "procurement.vendor.view",
                          tenant=self.tenant, role_key="role-no-fin-spend")
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/reports/dashboard/spend/?entity={self.books.code}",
        )
        self.assertEqual(response.status_code, 403)
