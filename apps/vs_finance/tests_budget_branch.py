"""Budgets for a branch-bound reader: the school's plan, and their branch's own.

Corona plans 500,000 kobo of operating revenue for the year. Ikeja invoices
100,000 and Lekki 300,000. The Ikeja bursar may read budgets, and sees the
school's plan. What they must not see is the actual set against it: the
school-wide 400,000 reports Lekki's income to them, and Ikeja's own 100,000
against the whole school's 500,000 would read as an 80% shortfall that is only
the other branches' share. A school-wide reader sees the plan and the actuals.

Ikeja may also build its own plan. That plan is measured against Ikeja's
journals only, so the bursar sees its actuals in full; Lekki's plan is not in
their reach at all, and the school's plan stays read-only to them.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, Budget, FiscalYear, InvoiceLine
from vs_finance.reports import budget_vs_actual, income_statement_compare

from .tests_branch_scope import _FinanceBranchFixture


class _BudgetFixture(_FinanceBranchFixture):
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


class BudgetActualsForBranchReadersTests(_BudgetFixture):
    """The school's plan, read by a branch-bound reader and by a school-wide one."""

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


class BranchBudgetTests(_BudgetFixture):
    """Each branch plans for itself, and reads and changes only what is its own."""

    def setUp(self):
        from vs_finance.budgets import create_budget
        from vs_finance.receivables import post_invoice

        super().setUp()
        e = self.books
        self.income = Account.objects.get(entity=e, code="4100")
        # A school-wide invoice, which no branch plan may count as its own.
        shared = self.invoice(e, self.customer(e, "CALL", None), None)
        InvoiceLine.objects.filter(invoice=shared).update(revenue_account=self.income)
        post_invoice(shared)

        year = FiscalYear.objects.get(entity=e, year=2026)
        plan = [{"account": self.income, "cost_center": None, "period_no": 1, "amount": 200_000}]
        self.ikeja_plan = create_budget(e, name="Ikeja plan", fiscal_year=year, lines=plan, branch=self.ikeja)
        self.lekki_plan = create_budget(e, name="Lekki plan", fiscal_year=year, lines=plan, branch=self.lekki)
        self.ikeja_client = self.client_holding(
            "plan-ikeja@corona.test", "finance.budget.view", "finance.budget.create",
            "finance.budget.edit", branch=self.ikeja,
        )

    def post(self, client, path, body):
        return client.post(f"/v1/finance/{path}?entity={self.books.code}", body, format="json")

    def test_a_branch_plan_is_measured_against_that_branchs_journals_only(self):
        report = budget_vs_actual(self.ikeja_plan)
        self.assertEqual(report.total_actual, 100_000)  # Not Lekki's, not the school-wide invoice.

    def test_the_branch_reader_lists_their_plan_and_the_schools_but_not_lekkis(self):
        rows = {row["id"]: row for row in self.get(self.ikeja_client, "budgets/")["data"]}

        self.assertEqual(set(rows), {self.budget.id, self.ikeja_plan.id})
        own, school = rows[self.ikeja_plan.id], rows[self.budget.id]
        self.assertEqual((own["branch_id"], own["branch_name"]), (self.ikeja.id, "Ikeja Branch"))
        self.assertTrue(own["can_manage"])
        self.assertEqual(own["actual_ytd"], 100_000)
        self.assertEqual(own["consumed_pct"], 50.0)
        self.assertIsNone(school["branch_id"])
        self.assertFalse(school["can_manage"])
        self.assertIsNone(school["actual_ytd"])

    def test_the_list_offers_each_reader_only_the_plans_they_may_file(self):
        filing = self.get(self.ikeja_client, "budgets/")["filing"]
        self.assertEqual(filing, {"school": False, "branches": [{"id": self.ikeja.id, "name": "Ikeja Branch"}]})

        school = self.get(self.client_holding("filing-hq@corona.test", "finance.budget.view"), "budgets/")["filing"]
        self.assertTrue(school["school"])
        self.assertEqual({b["id"] for b in school["branches"]}, {self.ikeja.id, self.lekki.id, self.yaba.id})

    def test_the_branch_reader_gets_the_full_variance_of_their_own_plan(self):
        data = self.get(self.ikeja_client, f"budgets/{self.ikeja_plan.pk}/variance/")["data"]
        self.assertFalse(data["narrowed"])
        self.assertEqual(data["total_actual"]["kobo"], 100_000)

    def test_another_branchs_plan_is_not_found(self):
        response = self.ikeja_client.get(f"/v1/finance/budgets/{self.lekki_plan.pk}/?entity={self.books.code}")
        self.assertEqual(response.status_code, 404)

    def test_what_a_branch_reader_creates_is_filed_to_their_branch(self):
        response = self.post(self.ikeja_client, "budgets/", {"name": "Ikeja capex", "fiscal_year": 2026})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Budget.objects.get(name="Ikeja capex").branch_id, self.ikeja.id)

    def test_a_branch_reader_cannot_file_a_plan_for_another_branch(self):
        response = self.post(
            self.ikeja_client, "budgets/", {"name": "Lekki grab", "fiscal_year": 2026, "branch": self.lekki.id},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Budget.objects.filter(name="Lekki grab").exists())

    def test_the_schools_plan_is_read_only_to_a_branch_reader(self):
        rename = self.ikeja_client.patch(
            f"/v1/finance/budgets/{self.budget.pk}/?entity={self.books.code}", {"name": "Mine now"}, format="json",
        )
        self.assertEqual(rename.status_code, 403)
        line = self.post(self.ikeja_client, f"budgets/{self.budget.pk}/lines/",
                         {"account": self.income.id, "period_no": 1, "amount": 1})
        self.assertEqual(line.status_code, 403)
        self.budget.refresh_from_db()
        self.assertEqual(self.budget.name, "Operating plan")

    def test_a_branch_reader_edits_their_own_plan(self):
        response = self.ikeja_client.patch(
            f"/v1/finance/budgets/{self.ikeja_plan.pk}/?entity={self.books.code}",
            {"name": "Ikeja operating plan"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_school_wide_reader_files_a_plan_for_a_branch_or_for_the_school(self):
        client = self.client_holding("plan-hq@corona.test", "finance.budget.view", "finance.budget.create")
        self.assertEqual(self.post(client, "budgets/", {
            "name": "Yaba plan", "fiscal_year": 2026, "branch": self.yaba.id}).status_code, 201)
        self.assertEqual(self.post(client, "budgets/", {
            "name": "Capex", "fiscal_year": 2026}).status_code, 201)
        self.assertEqual(Budget.objects.get(name="Yaba plan").branch_id, self.yaba.id)
        self.assertIsNone(Budget.objects.get(name="Capex").branch_id)

    def test_a_name_is_unique_within_one_school_or_branch_each_year(self):
        taken = self.post(self.ikeja_client, "budgets/", {"name": "Ikeja plan", "fiscal_year": 2026})
        self.assertEqual(taken.status_code, 422)
        self.assertIn("Ikeja Branch already has", str(taken.data))
        # The school's plan name is free for a branch to reuse.
        reused = self.post(self.ikeja_client, "budgets/", {"name": "Operating plan", "fiscal_year": 2026})
        self.assertEqual(reused.status_code, 201, reused.data)

    def test_the_income_statement_compares_against_the_schools_plan_only(self):
        self.budget.delete()
        self.assertFalse(income_statement_compare(self.books).has_budget)
