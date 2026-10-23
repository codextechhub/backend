"""Field Access on plans and statement sections, including alternate read paths."""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_branch_ledger import _LedgerFixture
from .tests_budget_branch import _BudgetFixture


class BudgetFieldAccessTests(_BudgetFixture):
    """A branch reader sees coordinates while a sensitive amount stays closed."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("finance.budget", "finance.report")
        cls.hidden_user = cls.user_holding(
            "budget-hidden@corona.test", "finance.budget.view", "finance.budget.create",
            "finance.budget.edit", "finance.audit.view", branch=cls.ikeja,
        )
        cls.open_user = cls.user_holding(
            "budget-open@corona.test", "finance.budget.view", "finance.budget.create",
            "finance.budget.edit", branch=cls.ikeja,
        )
        role = TenantRoleTemplate.objects.get(tenant=cls.tenant,
                                              key="role-budget-open@corona.test")
        set_field_access(role, "finance.budget.amount", read=True, write=True)

    def test_default_closed_amount_does_not_leak_through_budget_reads(self):
        client = TenantAPIClient(user=self.hidden_user)
        base = f"/v1/finance/budgets/?entity={self.books.code}"
        listing = client.get(base)
        self.assertEqual(listing.status_code, 200, listing.data)
        row = listing.data["data"][0]
        self.assertIn("name", row)
        self.assertNotIn("budgeted_total", row)
        self.assertNotIn("consumed_pct", row)
        detail = client.get(f"/v1/finance/budgets/{self.ikeja_plan.pk}/?entity={self.books.code}")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertIn("account", detail.data["data"]["lines"][0])
        self.assertNotIn("amount", detail.data["data"]["lines"][0])
        variance = client.get(
            f"/v1/finance/budgets/{self.ikeja_plan.pk}/variance/?entity={self.books.code}")
        self.assertEqual(variance.status_code, 200, variance.data)
        self.assertNotIn("total_budget", variance.data["data"])
        self.assertNotIn("variance", variance.data["data"]["rows"][0])
        heatmap = client.get(
            f"/v1/finance/budgets/{self.ikeja_plan.pk}/heatmap/?entity={self.books.code}")
        self.assertEqual(heatmap.status_code, 200, heatmap.data)
        self.assertNotIn("budget_total", heatmap.data["data"]["rows"][0])
        self.assertNotIn("cells", heatmap.data["data"]["rows"][0])

    def test_closed_amount_refuses_create_and_line_update(self):
        client = TenantAPIClient(user=self.hidden_user)
        create = client.post(f"/v1/finance/budgets/?entity={self.books.code}", {
            "name": "Hidden plan", "fiscal_year": 2026,
            "lines": [{"account": self.income.code, "period_no": 1, "amount": 900}],
        }, format="json")
        self.assertEqual(create.status_code, 403, create.data)
        replace = client.put(
            f"/v1/finance/budgets/{self.ikeja_plan.pk}/lines/?entity={self.books.code}",
            {"lines": [{"account": self.income.code, "period_no": 1, "amount": 900}]},
            format="json",
        )
        self.assertEqual(replace.status_code, 403, replace.data)

    def test_granted_amount_reads_and_writes(self):
        client = TenantAPIClient(user=self.open_user)
        row = client.get(f"/v1/finance/budgets/?entity={self.books.code}").data["data"][0]
        self.assertEqual(row["budgeted_total"], 200_000)
        self.assertEqual(row["consumed_pct"], 50.0)

    def test_hidden_budget_name_is_absent_from_existing_audit_sentences(self):
        from .models import FinanceAuditAction, FinanceAuditLog

        role = TenantRoleTemplate.objects.get(tenant=self.tenant,
                                              key="role-budget-hidden@corona.test")
        set_field_access(role, "finance.budget.name", read=False, write=False)
        FinanceAuditLog.objects.create(
            entity=self.books, branch=self.ikeja,
            action=FinanceAuditAction.BUDGET_APPROVED,
            target_type="Budget", target_id=str(self.ikeja_plan.pk),
            message="Approved budget 'Ikeja plan' for FY2026.",
        )
        response = TenantAPIClient(user=self.hidden_user).get(
            f"/v1/finance/audit-logs/?target_type=Budget&entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"])
        self.assertNotIn("Ikeja plan", str(response.data["data"]))


class ReportSectionFieldAccessTests(_LedgerFixture):
    """The same statement section is hidden in JSON and file downloads."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("finance.budget", "finance.report")
        cls.viewer = cls.grant(
            cls.user_for(cls.tenant, "reports-hidden@corona.test"),
            "finance.report.view", tenant=cls.tenant, role_key="reports-hidden",
        )
        cls.role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="reports-hidden")

    def api(self):
        return TenantAPIClient(user=self.viewer)

    def url(self, report, suffix=""):
        return f"/v1/finance/reports/{report}/?entity={self.books.code}{suffix}"

    def test_hidden_income_expenses_disappear_from_json_and_csv(self):
        set_field_access(self.role, "finance.report.income_expense", read=False)
        client = self.api()
        result = client.get(self.url("income-statement"))
        self.assertEqual(result.status_code, 200, result.data)
        data = result.data["data"]
        self.assertIn("income", data)
        self.assertNotIn("expense", data)
        self.assertNotIn("net", data["totals"])
        csv = client.get(self.url("income-statement", "&export=csv"))
        self.assertEqual(csv.status_code, 200)
        self.assertNotIn("Total expenses", csv.content.decode())
        self.assertNotIn("Net income", csv.content.decode())

    def test_pack_refuses_a_closed_section(self):
        set_field_access(self.role, "finance.report.income_expense", read=False)
        result = self.api().get(self.url("statutory-pack"))
        self.assertEqual(result.status_code, 403, result.data)

    def test_aging_rows_and_totals_are_independent(self):
        set_field_access(self.role, "finance.report.aging_customers", read=False)
        result = self.api().get(self.url("ar-aging"))
        self.assertEqual(result.status_code, 200, result.data)
        self.assertNotIn("rows", result.data["data"])
        self.assertIn("bucket_totals", result.data["data"])

    def test_a_single_section_report_refuses_a_hidden_reader(self):
        set_field_access(self.role, "finance.report.trial_balance", read=False)
        self.assertEqual(self.api().get(self.url("trial-balance")).status_code, 403)
        self.assertEqual(self.api().get(self.url("trial-balance", "&export=csv")).status_code, 403)

    def test_balance_sheet_hides_assets_in_json_and_export(self):
        set_field_access(self.role, "finance.report.balance_assets", read=False)
        client = self.api()
        result = client.get(self.url("balance-sheet"))
        self.assertEqual(result.status_code, 200, result.data)
        data = result.data["data"]
        self.assertNotIn("total_assets", data)
        self.assertNotIn("is_balanced", data)
        self.assertFalse(any("assets" in row["key"] for row in data["sections"]))
        csv = client.get(self.url("balance-sheet", "&export=csv"))
        self.assertEqual(csv.status_code, 200)
        self.assertNotIn("Total assets", csv.content.decode())

    def test_cash_flow_hides_activity_from_json_and_export(self):
        set_field_access(self.role, "finance.report.cashflow_activities", read=False)
        client = self.api()
        result = client.get(self.url("cash-flow"))
        self.assertEqual(result.status_code, 200, result.data)
        data = result.data["data"]
        self.assertIn("opening_cash", data)
        self.assertNotIn("activity_lines", data)
        self.assertNotIn("net_change", data)
        csv = client.get(self.url("cash-flow", "&export=csv"))
        self.assertEqual(csv.status_code, 200)
        self.assertNotIn("Operating activities", csv.content.decode())

    def test_equity_requires_its_switch(self):
        set_field_access(self.role, "finance.report.equity_changes", read=False)
        self.assertEqual(self.api().get(self.url("changes-in-equity")).status_code, 403)
        self.assertEqual(self.api().get(self.url("changes-in-equity", "&export=csv")).status_code, 403)

    def test_aging_totals_are_hidden_without_hiding_customers(self):
        set_field_access(self.role, "finance.report.aging_totals", read=False)
        client = self.api()
        result = client.get(self.url("ar-aging"))
        self.assertEqual(result.status_code, 200, result.data)
        data = result.data["data"]
        self.assertIn("rows", data)
        self.assertNotIn("bucket_totals", data)
        csv = client.get(self.url("ar-aging", "&export=csv"))
        self.assertEqual(csv.status_code, 200)
        self.assertNotIn("TOTAL", csv.content.decode())

    def test_budget_comparison_requires_sensitive_amount_read(self):
        result = self.api().get(self.url("income-statement"))
        self.assertEqual(result.status_code, 200, result.data)
        data = result.data["data"]
        self.assertNotIn("has_budget", data)
        self.assertNotIn("budget", data["totals"]["income"])
        csv = self.api().get(self.url("income-statement", "&export=csv"))
        self.assertEqual(csv.status_code, 200)
        self.assertNotIn("Budget", csv.content.decode().splitlines()[0])
