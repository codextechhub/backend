"""Every budget belongs to a branch, and the school's plan is their roll-up.

Corona has Ikeja, Lekki and Yaba. Ikeja invoices 100,000 kobo of operating
revenue and Lekki 300,000. Ikeja plans 200,000 and Lekki 400,000.

* Mrs Adeyemi, the Ikeja bursar, reads and changes Ikeja's plan only. Lekki's
  plan, and a plan raised before every budget named a branch, answer 404 to them.
* Ikeja's plan is measured against Ikeja's journals: 100,000 of 200,000.
* Mr Bello, the whole-school bursar, sees every plan, and the school total is
  600,000 planned against the whole ledger's 400,000.
* A new plan names its branch: Mr Bello names one (400 if they name none), Mrs
  Adeyemi's is Ikeja's without asking, and Harbour Primary, with one branch,
  never has to say.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, Budget, FiscalYear, InvoiceLine
from vs_finance.reports import (
    budget_rollup, budget_vs_actual, income_statement_compare, rolled_up_budgets,
)
from vs_rbac.scoping import BranchScope

from .tests_branch_scope import _FinanceBranchFixture


class _BudgetFixture(_FinanceBranchFixture):
    def setUp(self):
        from vs_finance.budgets import create_budget

        super().setUp()
        e = self.books
        self.income = Account.objects.get(entity=e, code="4100")
        self.posted_invoice(e, self.customer(e, "CIKJ", self.ikeja), self.ikeja)
        lekki_parent = self.customer(e, "CLEK", self.lekki)
        for _ in range(3):
            self.posted_invoice(e, lekki_parent, self.lekki)

        self.year = FiscalYear.objects.get(entity=e, year=2026)
        self.ikeja_plan = create_budget(
            e, name="Ikeja plan", fiscal_year=self.year, branch=self.ikeja,
            lines=[self.line(200_000)])
        self.lekki_plan = create_budget(
            e, name="Lekki plan", fiscal_year=self.year, branch=self.lekki,
            lines=[self.line(400_000)])

        self.adeyemi = self.client_holding(
            "plan-ikeja@corona.test", "finance.budget.view", "finance.budget.create",
            "finance.budget.edit", branch=self.ikeja,
        )
        self.bello = self.client_holding(
            "plan-hq@corona.test", "finance.budget.view", "finance.budget.create",
            "finance.budget.edit",
        )

    def line(self, amount, account=None):
        return {"account": account or self.income, "cost_center": None, "period_no": 1, "amount": amount}

    def posted_invoice(self, entity, customer, branch):
        from vs_finance.receivables import post_invoice

        invoice = self.invoice(entity, customer, branch)
        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=entity, code="4100"))
        post_invoice(invoice)
        return invoice

    def client_holding(self, email, *keys, branch=None, tenant=None):
        tenant = tenant or self.tenant
        user = self.grant(
            self.user_for(tenant, email), *keys,
            tenant=tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def get(self, client, path, books=None):
        books = books or self.books
        response = client.get(f"/v1/finance/{path}?entity={books.code}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.json()

    def post(self, client, path, body, books=None):
        books = books or self.books
        return client.post(f"/v1/finance/{path}?entity={books.code}", body, format="json")

    def unbranched_plan(self):
        from vs_finance.budgets import create_budget

        return create_budget(
            self.books, name="Old school plan", fiscal_year=self.year, lines=[self.line(900_000)])


class BranchReadersSeeTheirOwnBudgetsTests(_BudgetFixture):
    """Budgets are read exclusively, like every other money record."""

    def test_a_branch_reader_lists_only_their_branchs_budget(self):
        old = self.unbranched_plan()
        body = self.get(self.adeyemi, "budgets/")

        self.assertTrue(body["narrowed"])
        self.assertEqual([row["id"] for row in body["data"]], [self.ikeja_plan.id])
        self.assertNotIn(old.id, [row["id"] for row in body["data"]])

    def test_their_budget_carries_its_actuals_in_full(self):
        row = self.get(self.adeyemi, "budgets/")["data"][0]

        self.assertEqual(row["actual_ytd"], 100_000)
        self.assertEqual(row["consumed_pct"], 50.0)
        self.assertTrue(row["can_manage"])

    def test_an_unbranched_budget_is_not_found_by_a_branch_reader(self):
        old = self.unbranched_plan()
        for path in ("", "variance/", "heatmap/"):
            response = self.adeyemi.get(f"/v1/finance/budgets/{old.pk}/{path}?entity={self.books.code}")
            self.assertEqual(response.status_code, 404, path)

    def test_another_branchs_budget_is_not_found(self):
        response = self.adeyemi.get(f"/v1/finance/budgets/{self.lekki_plan.pk}/?entity={self.books.code}")
        self.assertEqual(response.status_code, 404)

    def test_a_whole_school_reader_sees_every_budget_the_unbranched_one_included(self):
        old = self.unbranched_plan()
        body = self.get(self.bello, "budgets/")

        self.assertFalse(body["narrowed"])
        self.assertEqual(
            {row["id"] for row in body["data"]}, {self.ikeja_plan.id, self.lekki_plan.id, old.id})

    def test_the_variance_of_their_own_budget_is_whole(self):
        data = self.get(self.adeyemi, f"budgets/{self.ikeja_plan.pk}/variance/")["data"]

        self.assertEqual(data["total_budget"]["kobo"], 200_000)
        self.assertEqual(data["total_actual"]["kobo"], 100_000)
        self.assertNotIn("narrowed", data)


class RaisingABudgetNamesItsBranchTests(_BudgetFixture):

    def test_a_branch_reader_files_to_their_branch(self):
        response = self.post(self.adeyemi, "budgets/", {"name": "Ikeja capex", "fiscal_year": 2026})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Budget.objects.get(name="Ikeja capex").branch_id, self.ikeja.id)

    def test_a_branch_reader_cannot_file_for_another_branch(self):
        response = self.post(
            self.adeyemi, "budgets/", {"name": "Lekki grab", "fiscal_year": 2026, "branch": self.lekki.id})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Budget.objects.filter(name="Lekki grab").exists())

    def test_a_whole_school_reader_must_name_the_branch(self):
        response = self.post(self.bello, "budgets/", {"name": "Capex", "fiscal_year": 2026})

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("more than one", str(response.data))
        self.assertFalse(Budget.objects.filter(name="Capex").exists())

    def test_a_whole_school_reader_naming_a_branch_files_it_there(self):
        response = self.post(self.bello, "budgets/", {"name": "Yaba plan", "fiscal_year": 2026,
                                                      "branch": self.yaba.id})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Budget.objects.get(name="Yaba plan").branch_id, self.yaba.id)

    def test_a_one_branch_school_is_never_asked(self):
        harbour = self.client_holding(
            "plan-solo@harbour.test", "finance.budget.view", "finance.budget.create",
            tenant=self.solo_tenant,
        )
        response = self.post(harbour, "budgets/", {"name": "Main plan", "fiscal_year": 2026},
                             books=self.solo_books)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Budget.objects.get(name="Main plan").branch_id, self.solo_main.id)

    def test_the_filing_choices_are_the_readers_branches_only(self):
        self.assertEqual(
            self.get(self.adeyemi, "budgets/")["filing"],
            {"branches": [{"id": self.ikeja.id, "name": "Ikeja Branch"}]},
        )
        whole = self.get(self.bello, "budgets/")["filing"]
        self.assertEqual({b["id"] for b in whole["branches"]}, {self.ikeja.id, self.lekki.id, self.yaba.id})

    def test_a_name_is_unique_within_one_branch_each_year(self):
        taken = self.post(self.adeyemi, "budgets/", {"name": "Ikeja plan", "fiscal_year": 2026})
        self.assertEqual(taken.status_code, 422)
        self.assertIn("Ikeja Branch already has", str(taken.data))
        reused = self.post(self.bello, "budgets/", {"name": "Ikeja plan", "fiscal_year": 2026,
                                                    "branch": self.lekki.id})
        self.assertEqual(reused.status_code, 201, reused.data)


class BudgetActualsTests(_BudgetFixture):
    """Each budget is measured against its own branch's journals."""

    def test_a_branch_plan_counts_only_its_branchs_journals(self):
        self.assertEqual(budget_vs_actual(self.ikeja_plan).total_actual, 100_000)
        self.assertEqual(budget_vs_actual(self.lekki_plan).total_actual, 300_000)

    def test_a_one_branch_schools_plan_counts_journals_not_yet_given_a_branch(self):
        """Harbour's invoices raised before invoices named a branch are Main's."""
        from vs_finance.budgets import create_budget

        e = self.solo_books
        self.posted_invoice(e, self.customer(e, "CSOLO", None), None)
        plan = create_budget(
            e, name="Main plan", fiscal_year=FiscalYear.objects.get(entity=e, year=2026),
            branch=self.solo_main,
            lines=[{"account": Account.objects.get(entity=e, code="4100"), "cost_center": None,
                    "period_no": 1, "amount": 500_000}],
        )

        self.assertEqual(budget_vs_actual(plan).total_actual, 100_000)


class SchoolTotalTests(_BudgetFixture):
    """The school's plan is the sum of its branches' plans."""

    def test_the_whole_school_total_sums_every_branchs_plan(self):
        data = self.get(self.bello, "budgets/rollup/")["data"]

        self.assertEqual(data["fiscal_year"], 2026)
        self.assertEqual(data["total_budget"]["kobo"], 600_000)
        self.assertEqual(data["total_actual"]["kobo"], 400_000)
        self.assertEqual({b["id"] for b in data["budgets"]}, {self.ikeja_plan.id, self.lekki_plan.id})
        self.assertFalse(data["narrowed"])

    def test_a_branch_readers_total_is_their_branchs_plan(self):
        data = self.get(self.adeyemi, "budgets/rollup/")["data"]

        self.assertEqual(data["total_budget"]["kobo"], 200_000)
        self.assertEqual(data["total_actual"]["kobo"], 100_000)
        self.assertEqual([b["id"] for b in data["budgets"]], [self.ikeja_plan.id])

    def test_each_branch_counts_once_approved_over_draft(self):
        from vs_finance.budgets import approve_budget, create_budget

        approve_budget(self.ikeja_plan)
        create_budget(self.books, name="Ikeja revision", fiscal_year=self.year,
                      branch=self.ikeja, lines=[self.line(999_000)])

        plans = rolled_up_budgets(self.books, self.year)
        self.assertEqual({p.id for p in plans}, {self.ikeja_plan.id, self.lekki_plan.id})

    def test_an_unbranched_plan_is_not_counted_beside_branch_plans(self):
        self.unbranched_plan()

        self.assertEqual(budget_rollup(self.books, self.year).total_budget, 600_000)

    def test_an_unbranched_plan_is_the_total_while_no_branch_has_planned(self):
        Budget.objects.filter(pk__in=[self.ikeja_plan.pk, self.lekki_plan.pk]).delete()
        old = self.unbranched_plan()

        report = budget_rollup(self.books, self.year)
        self.assertEqual([b.id for b in report.budgets], [old.id])
        self.assertEqual(report.total_budget, 900_000)
        narrowed = BranchScope(frozenset({self.ikeja.id}), include_shared=False)
        self.assertEqual(budget_rollup(self.books, self.year, scope=narrowed).budgets, [])

    def test_the_income_statement_compares_against_the_roll_up(self):
        whole = income_statement_compare(self.books, fiscal_year=self.year)
        self.assertTrue(whole.has_budget)
        self.assertEqual(whole.income_totals.budget, 600_000)

        ikeja = income_statement_compare(
            self.books, fiscal_year=self.year,
            scope=BranchScope(frozenset({self.ikeja.id}), include_shared=False))
        self.assertTrue(ikeja.has_budget)
        self.assertEqual(ikeja.income_totals.budget, 200_000)
        self.assertEqual(ikeja.income_totals.amount, 100_000)

    def test_the_overview_dashboard_plans_against_the_roll_up(self):
        from vs_finance.dashboard import _revenue_vs_budget

        card = _revenue_vs_budget(self.books, self.year)
        self.assertTrue(card["has_budget"])
        self.assertEqual(card["revenue"]["plan"]["kobo"], 600_000)
        self.assertEqual(card["budget_name"], "School total (2 budgets)")

    def test_the_roll_up_needs_a_budget_view_key(self):
        client = self.client_holding("no-budget@corona.test", "finance.report.view")
        response = client.get(f"/v1/finance/budgets/rollup/?entity={self.books.code}")
        self.assertEqual(response.status_code, 403)


class OneBranchSchoolBudgetTests(_BudgetFixture):
    """Harbour Primary keeps one plan and reads it exactly as before."""

    def test_a_pinned_reader_at_the_only_branch_sees_the_unbranched_plan(self):
        from vs_finance.budgets import create_budget

        e = self.solo_books
        old = create_budget(e, name="Harbour plan", fiscal_year=FiscalYear.objects.get(entity=e, year=2026))
        pinned = self.client_holding(
            "solo-pinned@harbour.test", "finance.budget.view",
            branch=self.solo_main, tenant=self.solo_tenant)

        body = self.get(pinned, "budgets/", books=e)
        self.assertEqual([row["id"] for row in body["data"]], [old.id])
        self.assertFalse(body["narrowed"])
