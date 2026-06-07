"""HTTP tests for the operational/setup REST surface at ``/v1/finance/``.

These drive the Phase-4 capabilities that Phase 5 had not yet wrapped in an envelope —
reference data, banking + reconciliation, expense claims, payroll, budgets, fixed assets
and the audit trail — through the real URLs as a Vision super admin (who bypasses RBAC).
They prove the thin views wire the existing services correctly: documents are created in
DRAFT, the service-owned journals post on the action endpoints, and the document state
(payment status, run status, asset status, accumulated depreciation) advances as the
ledger does.

Kept separate from ``tests.py`` (the pure-Python service contract tests) so that suite
stays envelope-free while this one exercises the API plumbing.
"""
from __future__ import annotations

import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from vs_finance.models import (
    Account,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies


class _FinanceAPIFixture:
    """Seeds an entity (chart + an open full-year period) and a Vision super admin."""

    def build(self):
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        today = datetime.date.today()
        year = FiscalYear.objects.create(
            entity=entity, year=today.year,
            start_date=datetime.date(today.year, 1, 1),
            end_date=datetime.date(today.year, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name=f"FY{today.year}",
            start_date=datetime.date(today.year, 1, 1),
            end_date=datetime.date(today.year, 12, 31),
        )
        return entity, year, today

    @staticmethod
    def acc(entity, code):
        return Account.objects.get(entity=entity, code=code)

    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        User = get_user_model()
        self.user = User.objects.create_user(
            email="fin-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Fin", last_name="Admin",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.user, role=role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _q(self, path):
        return f"/v1/finance/{path}?entity={self.entity.code}"


class SetupDataAPITests(_FinanceAPIFixture, TestCase):
    def test_currency_is_global_no_entity_needed(self):
        self.build()
        # Currencies are global reference data — no ?entity required.
        resp = self.client.post("/v1/finance/currencies/", {
            "code": "kes", "name": "Kenyan Shilling", "symbol": "KSh",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["code"], "KES")

        listing = self.client.get("/v1/finance/currencies/")
        self.assertEqual(listing.status_code, 200)
        codes = {c["code"] for c in listing.json()["data"]}
        self.assertIn("KES", codes)
        self.assertIn("NGN", codes)

    def test_tax_code_and_cost_center_are_entity_scoped(self):
        self.entity, _, _ = self.build()
        tax = self.client.post(self._q("tax-codes/"), {
            "code": "VAT", "name": "Output VAT 7.5%", "rate_bps": 750,
            "collected_account": "2200",
        }, format="json")
        self.assertEqual(tax.status_code, 201, tax.content)
        self.assertEqual(tax.json()["data"]["collected_account"], "2200")

        cc = self.client.post(self._q("cost-centers/"), {
            "code": "ADMIN", "name": "Administration",
        }, format="json")
        self.assertEqual(cc.status_code, 201, cc.content)

        listing = self.client.get(self._q("cost-centers/"))
        self.assertEqual({c["code"] for c in listing.json()["data"]}, {"ADMIN"})


class BankingAPITests(_FinanceAPIFixture, TestCase):
    def test_import_then_adjust_books_and_matches_line(self):
        self.entity, _, today = self.build()
        d = today.isoformat()

        bank = self.client.post(self._q("bank-accounts/"), {
            "name": "GTBank Operations", "bank_name": "GTBank",
            "account_number": "0123456789", "gl_account": "1100",
        }, format="json")
        self.assertEqual(bank.status_code, 201, bank.content)
        bank_id = bank.json()["data"]["id"]

        # Import one outflow the books don't know about yet (a -₦500 bank charge).
        imp = self.client.post(self._q(f"bank-accounts/{bank_id}/statement-lines/"), {
            "lines": [{"txn_date": d, "amount": -50000,
                       "description": "Monthly maintenance fee", "external_id": "TX-1"}],
        }, format="json")
        self.assertEqual(imp.status_code, 201, imp.content)
        line_id = imp.json()["data"][0]["id"]
        self.assertEqual(imp.json()["data"][0]["status"], "UNMATCHED")

        # Re-import the same external_id → deduped, nothing created.
        again = self.client.post(self._q(f"bank-accounts/{bank_id}/statement-lines/"), {
            "lines": [{"txn_date": d, "amount": -50000, "external_id": "TX-1"}],
        }, format="json")
        self.assertFalse(again.json()["data"])  # nothing created (empty envelope)

        # Adjust → books Dr 5500 Bank Charges / Cr 1100, and matches the line.
        adj = self.client.post(self._q(f"statement-lines/{line_id}/adjust/"),
                               {"narration": "Bank charge"}, format="json")
        self.assertEqual(adj.status_code, 201, adj.content)
        self.assertEqual(adj.json()["data"]["status"], "MATCHED")


class ExpenseClaimAPITests(_FinanceAPIFixture, TestCase):
    def test_create_post_settle(self):
        self.entity, _, today = self.build()
        d = today.isoformat()

        # Bank to reimburse from.
        bank_id = self.client.post(self._q("bank-accounts/"), {
            "name": "Petty Cash", "gl_account": "1100",
        }, format="json").json()["data"]["id"]

        create = self.client.post(self._q("expense-claims/"), {
            "claimant_name": "Jane Doe", "claim_date": d, "title": "Conference travel",
            "lines": [{"description": "Taxi", "expense_account": "5300",
                       "quantity": 1, "unit_price": 250000}],
        }, format="json")
        self.assertEqual(create.status_code, 201, create.content)
        claim_id = create.json()["data"]["id"]
        self.assertEqual(create.json()["data"]["total"], 250000)
        self.assertEqual(create.json()["data"]["status"], "DRAFT")

        posted = self.client.post(self._q(f"expense-claims/{claim_id}/post/"))
        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(posted.json()["data"]["status"], "POSTED")

        settle = self.client.post(self._q(f"expense-claims/{claim_id}/settle/"), {
            "bank_account": bank_id, "pay_date": d,
        }, format="json")
        self.assertEqual(settle.status_code, 200, settle.content)
        self.assertEqual(settle.json()["data"]["payment_status"], "PAID")
        self.assertEqual(settle.json()["data"]["balance_due"], 0)


class PayrollAPITests(_FinanceAPIFixture, TestCase):
    def test_create_post_pay(self):
        self.entity, _, today = self.build()
        d = today.isoformat()
        bank_id = self.client.post(self._q("bank-accounts/"), {
            "name": "Payroll Account", "gl_account": "1100",
        }, format="json").json()["data"]["id"]

        create = self.client.post(self._q("payroll-runs/"), {
            "pay_date": d, "period_label": "Jan", "bank_account": bank_id,
            "lines": [{"employee_name": "Ada", "gross_amount": 1_000_000,
                       "paye_amount": 100000, "pension_amount": 80000}],
        }, format="json")
        self.assertEqual(create.status_code, 201, create.content)
        run_id = create.json()["data"]["id"]
        # net = gross - paye - pension = 820,000.
        self.assertEqual(create.json()["data"]["net_total"], 820000)

        posted = self.client.post(self._q(f"payroll-runs/{run_id}/post/"))
        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(posted.json()["data"]["run_status"], "POSTED")

        paid = self.client.post(self._q(f"payroll-runs/{run_id}/pay/"), {}, format="json")
        self.assertEqual(paid.status_code, 200, paid.content)
        self.assertEqual(paid.json()["data"]["run_status"], "PAID")


class BudgetAPITests(_FinanceAPIFixture, TestCase):
    def test_create_line_variance_approve_lock(self):
        self.entity, year, _ = self.build()

        create = self.client.post(self._q("budgets/"), {
            "fiscal_year": year.year, "name": "FY Operating Budget",
        }, format="json")
        self.assertEqual(create.status_code, 201, create.content)
        budget_id = create.json()["data"]["id"]

        line = self.client.post(self._q(f"budgets/{budget_id}/lines/"), {
            "account": "5300", "period_no": 1, "amount": 500000,
        }, format="json")
        self.assertEqual(line.status_code, 201, line.content)

        variance = self.client.get(self._q(f"budgets/{budget_id}/variance/"))
        self.assertEqual(variance.status_code, 200, variance.content)
        self.assertEqual(variance.json()["data"]["total_budget"]["kobo"], 500000)

        approve = self.client.post(self._q(f"budgets/{budget_id}/approve/"))
        self.assertEqual(approve.status_code, 200, approve.content)
        self.assertEqual(approve.json()["data"]["status"], "APPROVED")
        self.assertTrue(approve.json()["data"]["is_locked"])

        # Locked → further edits rejected by the service (domain error → 4xx).
        blocked = self.client.post(self._q(f"budgets/{budget_id}/lines/"), {
            "account": "5300", "period_no": 2, "amount": 100000,
        }, format="json")
        self.assertGreaterEqual(blocked.status_code, 400)
        self.assertLess(blocked.status_code, 500)


class FixedAssetAPITests(_FinanceAPIFixture, TestCase):
    def test_create_acquire_depreciate(self):
        self.entity, _, today = self.build()
        bank_id = self.client.post(self._q("bank-accounts/"), {
            "name": "Capex Account", "gl_account": "1100",
        }, format="json").json()["data"]["id"]

        create = self.client.post(self._q("fixed-assets/"), {
            "name": "Server Rack", "asset_code": "IT-001",
            "acquisition_date": datetime.date(today.year, 1, 1).isoformat(),
            "cost": 12_000_000, "salvage_value": 0, "useful_life_months": 12,
        }, format="json")
        self.assertEqual(create.status_code, 201, create.content)
        asset_id = create.json()["data"]["id"]
        self.assertEqual(create.json()["data"]["asset_status"], "DRAFT")

        acquire = self.client.post(self._q(f"fixed-assets/{asset_id}/acquire/"), {
            "bank_account": bank_id,
        }, format="json")
        self.assertEqual(acquire.status_code, 200, acquire.content)
        self.assertEqual(acquire.json()["data"]["asset_status"], "ACTIVE")
        self.assertEqual(len(acquire.json()["data"]["schedule"]), 12)

        # Depreciate every charge due within the (single open) fiscal year.
        dep = self.client.post(self._q(f"fixed-assets/{asset_id}/depreciate/"), {
            "up_to_date": datetime.date(today.year, 12, 31).isoformat(),
        }, format="json")
        self.assertEqual(dep.status_code, 200, dep.content)
        self.assertGreater(dep.json()["data"]["accumulated_depreciation"], 0)
        self.assertLess(
            dep.json()["data"]["net_book_value"], 12_000_000,
        )


class AuditLogAPITests(_FinanceAPIFixture, TestCase):
    def test_audit_trail_records_actions(self):
        self.entity, _, today = self.build()
        d = today.isoformat()
        bank_id = self.client.post(self._q("bank-accounts/"), {
            "name": "Ops", "gl_account": "1100",
        }, format="json").json()["data"]["id"]
        claim_id = self.client.post(self._q("expense-claims/"), {
            "claimant_name": "Sam", "claim_date": d,
            "lines": [{"description": "Stationery", "expense_account": "5300",
                       "quantity": 1, "unit_price": 30000}],
        }, format="json").json()["data"]["id"]
        self.client.post(self._q(f"expense-claims/{claim_id}/post/"))

        logs = self.client.get(self._q("audit-logs/"))
        self.assertEqual(logs.status_code, 200, logs.content)
        actions = {row["action"] for row in logs.json()["data"]}
        self.assertTrue(actions, "expected at least one audit entry after posting a claim")


class FinanceOpsAuthTests(_FinanceAPIFixture, TestCase):
    def test_entity_scoped_endpoint_requires_entity(self):
        self.build()
        resp = self.client.get("/v1/finance/tax-codes/")
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_is_rejected(self):
        self.build()
        anon = APIClient()
        resp = anon.get("/v1/finance/currencies/")
        self.assertIn(resp.status_code, (401, 403))
