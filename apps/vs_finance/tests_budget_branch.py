"""The Budgets screen for a branch-bound reader: the plan, without the actuals.

Corona plans 500,000 kobo of operating revenue for the year. Ikeja invoices
100,000 and Lekki 300,000. The Ikeja bursar may read budgets, and sees that
plan. What he must not see is the actual set against it: the school-wide
400,000 reports Lekki's income to him, and Ikeja's own 100,000 against the whole
school's 500,000 would read as an 80% shortfall that is only the other
branches' share. A school-wide reader sees the plan and the actuals as before.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, FiscalYear, InvoiceLine
from vs_finance.reports import budget_vs_actual

from .tests_branch_scope import _FinanceBranchFixture


class BudgetActualsForBranchReadersTests(_FinanceBranchFixture):
    def setUp(self):
        from vs_finance.budgets import create_budget
        from vs_finance.receivables import post_invoice

        super().setUp()
        e = self.books
        income = Account.objects.get(entity=e, code="4100")

        def posted_invoice(customer, branch):
            invoice = self.invoice(e, customer, branch)
            InvoiceLine.objects.filter(invoice=invoice).update(revenue_account=income)
            post_invoice(invoice)

        posted_invoice(self.customer(e, "CIKJ", self.ikeja), self.ikeja)
        lekki_parent = self.customer(e, "CLEK", self.lekki)
        for _ in range(3):
            posted_invoice(lekki_parent, self.lekki)
        # Activity on an account the plan does not cover.
        other_income = Account.objects.filter(entity=e, account_type="INCOME", is_postable=True) \
            .exclude(pk=income.pk).first()
        if other_income is not None:
            invoice = self.invoice(e, self.customer(e, "COTH", self.lekki), self.lekki)
            InvoiceLine.objects.filter(invoice=invoice).update(revenue_account=other_income)
            post_invoice(invoice)
        self.other_income = other_income

        self.budget = create_budget(
            e, name="Operating plan", fiscal_year=FiscalYear.objects.get(entity=e, year=2026),
            lines=[{"account": income, "cost_center": None, "period_no": 1, "amount": 500_000}],
        )

    def client_holding(self, email, *keys, branch=None):
        user = self.grant(
            self.user_for(self.tenant, email), *keys,
            tenant=self.tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def get(self, client, path):
        response = client.get(f"/v1/finance/{path}?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.json()

    def test_a_branch_reader_sees_the_plan_but_no_actuals_in_the_list(self):
        client = self.client_holding("budget-ikeja@corona.test", "finance.budget.view", branch=self.ikeja)
        body = self.get(client, "budgets/")

        self.assertTrue(body["narrowed"])
        row = body["data"][0]
        self.assertEqual(row["budgeted_total"], 500_000)
        self.assertIsNone(row["actual_ytd"])
        self.assertIsNone(row["consumed_pct"])

    def test_a_branch_reader_gets_no_actual_or_variance_and_no_unbudgeted_rows(self):
        client = self.client_holding("variance-ikeja@corona.test", "finance.budget.view", branch=self.ikeja)
        data = self.get(client, f"budgets/{self.budget.pk}/variance/")["data"]

        self.assertTrue(data["narrowed"])
        self.assertIsNone(data["total_actual"])
        self.assertIsNone(data["total_variance"])
        self.assertEqual(data["total_budget"]["kobo"], 500_000)
        self.assertTrue(all(row["actual"] is None for row in data["rows"]))
        if self.other_income is not None:
            self.assertNotIn(self.other_income.id, {row["account_id"] for row in data["rows"]})

    def test_a_branch_reader_gets_a_heatmap_of_the_plan_only(self):
        client = self.client_holding("heat-ikeja@corona.test", "finance.budget.view", branch=self.ikeja)
        data = self.get(client, f"budgets/{self.budget.pk}/heatmap/")["data"]

        self.assertTrue(data["narrowed"])
        self.assertIsNone(data["total_actual"])
        cells = [cell for row in data["rows"] for cell in row["cells"]]
        self.assertTrue(cells)
        self.assertTrue(all(cell["actual"] is None for cell in cells))

    def test_a_school_wide_reader_sees_the_actuals(self):
        client = self.client_holding("budget-hq@corona.test", "finance.budget.view")
        body = self.get(client, "budgets/")
        self.assertFalse(body["narrowed"])
        self.assertEqual(body["data"][0]["actual_ytd"], budget_vs_actual(self.budget).total_actual)

        variance = self.get(client, f"budgets/{self.budget.pk}/variance/")["data"]
        self.assertEqual(variance["total_budget"]["kobo"], 500_000)
        self.assertIsNotNone(variance["total_actual"])
