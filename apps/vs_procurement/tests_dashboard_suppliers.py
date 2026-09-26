"""The Spend & suppliers tab.

The same two-branch school as the overview tests, read as at 31 January 2026,
with a little more history: the desks were expected on 6 January and arrived on
the 8th (late); the chairs were expected on the 10th and arrived on the 8th (on
time), with one chair rejected. The school also pays a 6,000 bill with no order.

Spend is read against the approved school plan on the accounts purchasing posts
to; a branch reader never gets that comparison, since a school plan measures the
whole school.
"""
from __future__ import annotations

import datetime

from vs_finance.models import Budget, BudgetLine

from .dashboard_suppliers import suppliers_view
from .models import GoodsReceivedNoteLine, PurchaseOrder, VendorAssessment, VendorQuotation
from .tests_dashboard_overview import AS_OF, _OverviewFixture


class _SuppliersFixture(_OverviewFixture):
    def setUp(self):
        super().setUp()
        PurchaseOrder.objects.filter(pk=self.desks.pk).update(expected_date=datetime.date(2026, 1, 6))
        PurchaseOrder.objects.filter(pk=self.chairs.pk).update(expected_date=datetime.date(2026, 1, 10))
        GoodsReceivedNoteLine.objects.filter(grn__purchase_order=self.chairs).update(rejected_qty=1)
        self.direct = self.make_bill(self.multi.entity, self.multi.vendor, [("5300", 1, 6_000, None, None)])
        from .payables import post_vendor_invoice, price_vendor_invoice

        price_vendor_invoice(self.direct)
        post_vendor_invoice(self.direct)

    def view(self, reader=None, branch=None, window="month"):
        from vs_finance.dashboard import EVERY_BLOCK
        from vs_rbac.scoping import BranchScope

        from .views.base import _BranchScope

        scope = _BranchScope(BranchScope(frozenset({branch.id}), include_shared=False), {}) if branch else None
        return suppliers_view(self.multi.entity, as_of=AS_OF, reader=reader or EVERY_BLOCK,
                              branch_scope=scope, window=window)


class PlanTests(_SuppliersFixture):
    def plan(self, amount):
        budget = Budget.objects.create(entity=self.multi.entity, fiscal_year=self.period_year(), name="Plan",
                                       status="APPROVED")
        BudgetLine.objects.create(budget=budget, account=self.acc(self.multi.entity, "5300"),
                                  period_no=1, amount=amount)

    def period_year(self):
        from vs_finance.models import FiscalYear

        return FiscalYear.objects.get(entity=self.multi.entity, year=2026)

    def test_spend_reads_against_the_approved_plan_on_purchasing_accounts(self):
        self.plan(1_000_000)
        plan = self.view()["spend"]["plan"]
        # Net of tax: desks billed 110,000, uniforms 6,000, the direct bill 6,000.
        self.assertEqual((plan["planned"]["kobo"], plan["spent_ytd"]["kobo"]), (1_000_000, 122_000))
        self.assertEqual(plan["pct"], 12.2)

    def test_no_approved_plan_means_no_plan_figure(self):
        self.assertIsNone(self.view()["spend"]["plan"])

    def test_a_branch_reader_never_gets_the_school_plan(self):
        self.plan(1_000_000)
        self.assertIsNone(self.view(branch=self.ikeja)["spend"]["plan"])


class DeliveryAndBillingTests(_SuppliersFixture):
    def test_on_time_and_accepted_shares(self):
        d = self.view()["deliveries"]
        self.assertEqual(d["on_time_pct"], 50.0)  # Chairs on time, desks late; lamps and uniforms unrated.
        self.assertEqual(d["rejected_lines"], 1)

    def test_spend_without_an_order_against_the_schools_limit(self):
        non_po = self.view()["non_po"]
        self.assertEqual((non_po["amount"]["kobo"], non_po["count"], non_po["limit_pct"]), (6_000, 1, 2))
        self.assertEqual(non_po["pct"], round(6_000 * 100 / (110_000 + 6_000 + 6_000), 1))

    def test_the_bill_to_payment_step_is_measured(self):
        from .models import VendorPayment, VendorPaymentAllocation
        from .payables import post_vendor_payment
        from vs_finance.models import Account

        invoice = self.direct
        payment = VendorPayment.objects.create(
            entity=self.multi.entity, vendor=invoice.vendor, payment_date=datetime.date(2026, 1, 20),
            method="BANK_TRANSFER", payment_account=Account.objects.get(entity=self.multi.entity, code="1100"),
            gross_amount=6_000, net_amount=6_000, approval_state="APPROVED",
        )
        VendorPaymentAllocation.objects.create(payment=payment, vendor_invoice=invoice, amount=6_000)
        post_vendor_payment(payment, auto_allocate=False)
        steps = {s["key"]: s for s in self.view()["cycle_times"]["steps"]}
        self.assertEqual(steps["payment"]["median_days"], 10)  # Billed 10 January, paid the 20th.


class VendorTests(_SuppliersFixture):
    def test_the_scorecard_carries_the_latest_grade(self):
        VendorAssessment.objects.create(entity=self.multi.entity, vendor=self.multi.vendor, on_time_delivery=95,
                                        quality_acceptance=95, invoice_accuracy=95, responsiveness=95)
        row = next(r for r in self.view()["scorecard"] if r["name"] == "Acme Supplies")
        self.assertEqual(row["grade"], "A")
        self.assertEqual(row["open_orders"], 2)

    def test_competition_saving_is_the_awarded_price_below_the_highest_quote(self):
        from .models import RequestForQuotation

        rfq = RequestForQuotation.objects.create(entity=self.multi.entity, title="Tyres", rfq_status="AWARDED",
                                                 issue_date=datetime.date(2026, 1, 2))
        for status, total in (("AWARDED", 80_000), ("REJECTED", 100_000)):
            VendorQuotation.objects.create(
                entity=self.multi.entity, rfq=rfq, vendor=self.multi.vendor, quotation_status=status, total=total,
                quote_date=datetime.date(2026, 1, 3), awarded_po=self.desks if status == "AWARDED" else None,
            )
        savings = self.view()["savings"]
        self.assertEqual((savings["saved"]["kobo"], savings["pct"], savings["rfqs"]), (20_000, 20.0, 1))

    def test_blocks_follow_their_keys(self):
        d = self.view(reader=self.keys("procurement.requisition.view"))
        for block in ("spend", "vendors_paid", "deliveries", "non_po", "scorecard", "open_rfqs", "savings",
                      "by_branch", "cycle_times", "vendor_base"):
            self.assertIsNone(d[block], block)


class SuppliersEndpointTests(_SuppliersFixture):
    def test_the_endpoint_opens_to_a_procurement_reader(self):
        client = self.client_for(self.multi_tenant, "sup-head@t.com")
        self.grant(client.test_user, "procurement.analytics.view", tenant=self.multi_tenant, role_key="role-sup")
        response = client.get(f"/v1/procurement/reports/dashboard/suppliers/?entity={self.multi.entity.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.json()["data"]["scorecard"])

    def test_a_reader_with_no_procurement_key_is_refused(self):
        client = self.client_for(self.multi_tenant, "fin-only-sup@t.com")
        self.grant(client.test_user, "finance.invoice.view", tenant=self.multi_tenant, role_key="role-fin-sup")
        response = client.get(f"/v1/procurement/reports/dashboard/suppliers/?entity={self.multi.entity.code}")
        self.assertEqual(response.status_code, 403)
