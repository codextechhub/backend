"""The Procurement overview's pipeline, exceptions, bills falling due and contracts.

A two-branch school, read as at 31 January 2026:

- Lekki orders 10 chairs at 10,000 and has received 4; nothing is billed yet.
- Ikeja orders 5 desks at 20,000 on the cleaning contract, receives all 5, and is
  billed at 22,000 a desk: above the order price. The bill fell due on 10 January.
- Lekki orders 4 lamps, receives 2 and is billed for 3: the match fails and the
  bill waits as a draft.
- A second vendor has a posted bill and is then put on hold.

Each pipeline stage needs the key of its list; a Lekki reader sees Lekki's
documents only, and a contract's ordered value (every branch's orders on it) is
withheld from them.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.dashboard import DashboardReader, EVERY_BLOCK
from vs_rbac.scoping import BranchScope

from .dashboard import procurement_dashboard
from .models import GoodsReceivedNote, GoodsReceivedNoteLine, PurchaseOrder, PurchaseOrderLine, Vendor
from .tests import _BranchTenantsFixture
from .views.base import _BranchScope

AS_OF = datetime.date(2026, 1, 31)


class _OverviewFixture(_BranchTenantsFixture, TestCase):
    def setUp(self):
        from .contracts import activate_contract
        from .models import VendorContract

        super().setUp()
        e = self.multi.entity
        self.contract = VendorContract.objects.create(
            entity=e, vendor=self.multi.vendor, reference="CLEAN", title="Cleaning services",
            start_date=datetime.date(2025, 3, 1), end_date=datetime.date(2026, 2, 20), contract_value=500_000,
        )
        activate_contract(self.contract)
        self.chairs = self.order(self.lekki, 10, 10_000)
        self.receive(self.chairs, 4)
        self.desks = self.order(self.ikeja, 5, 20_000, contract=self.contract)
        self.bill(self.desks, self.receive(self.desks, 5), 5, price=22_000)
        self.lamps = self.order(self.lekki, 4, 5_000)
        self.bill(self.lamps, self.receive(self.lamps, 2), 3, post=False)
        self.held = Vendor.objects.create(
            entity=e, code="HELD", name="Prime Uniforms", payable_account=self.acc(e, "2100"),
            default_expense_account=self.acc(e, "5300"), kyc_status="VERIFIED",
        )
        uniforms = self.order(self.ikeja, 2, 3_000, vendor=self.held)
        self.bill(uniforms, self.receive(uniforms, 2), 2)
        Vendor.objects.filter(pk=self.held.pk).update(on_hold=True)

    def order(self, branch, qty, price, *, contract=None, vendor=None):
        from .purchasing import price_po

        po = PurchaseOrder.objects.create(
            entity=self.multi.entity, vendor=vendor or self.multi.vendor, branch=branch, contract=contract,
            order_date=datetime.date(2026, 1, 5), status="APPROVED",
        )
        PurchaseOrderLine.objects.create(purchase_order=po, line_no=1, description="item", quantity=qty,
                                         unit_price=price, expense_account=self.acc(self.multi.entity, "5300"))
        price_po(po)
        return po

    def receive(self, po, qty):
        from .purchasing import post_grn

        grn = GoodsReceivedNote.objects.create(entity=po.entity, vendor=po.vendor, purchase_order=po,
                                               branch=po.branch, received_date=datetime.date(2026, 1, 8))
        line = po.lines.first()
        GoodsReceivedNoteLine.objects.create(grn=grn, po_line=line, expense_account=line.expense_account,
                                             accepted_qty=qty, unit_price=line.unit_price, line_no=1)
        post_grn(grn)
        return grn

    def bill(self, po, grn, qty, *, price=None, post=True):
        from .models import VendorInvoiceLine
        from .payables import match_vendor_invoice, post_vendor_invoice, price_vendor_invoice

        invoice = self.make_bill(po.entity, po.vendor, [], po=po)
        invoice.branch = po.branch
        invoice.save(update_fields=["branch"])
        line = po.lines.first()
        VendorInvoiceLine.objects.create(vendor_invoice=invoice, po_line=line, grn_line=grn.lines.first(),
                                         line_no=1, expense_account=line.expense_account, quantity=qty,
                                         unit_price=price or line.unit_price)
        price_vendor_invoice(invoice)
        match_vendor_invoice(invoice)
        if post:
            post_vendor_invoice(invoice, allow_variance=True)
        return invoice

    def view(self, reader=EVERY_BLOCK, branch=None, window=None):
        scope = _BranchScope(BranchScope(frozenset({branch.id}), include_shared=False), {}) if branch else None
        return procurement_dashboard(self.multi.entity, as_of=AS_OF, reader=reader, branch_scope=scope,
                                     window=window)

    def keys(self, *keys):
        return DashboardReader(keys=frozenset(keys))


class PipelineTests(_OverviewFixture):
    def test_each_stage_counts_what_is_open_now(self):
        stages = self.view()["pipeline"]
        self.assertEqual(stages["orders"]["count"], 2)  # Chairs and lamps are part-received.
        self.assertEqual(stages["orders"]["flag"], 2)
        self.assertEqual(stages["received_not_billed"]["amount"]["kobo"], 4 * 10_000 + 2 * 5_000)
        self.assertEqual(stages["bills"]["count"], 2)
        self.assertEqual(stages["bills"]["flag"], 2)  # Both fell due on 10 January.

    def test_a_stage_needs_the_key_of_its_list(self):
        stages = self.view(reader=self.keys("procurement.requisition.view"))["pipeline"]
        self.assertEqual(set(stages), {"requisitions"})

    def test_a_branch_reader_counts_only_their_branch(self):
        lekki = self.view(branch=self.lekki)["pipeline"]
        self.assertEqual(lekki["bills"]["count"], 0)
        self.assertEqual(lekki["received_not_billed"]["amount"]["kobo"], 50_000)


class ExceptionTests(_OverviewFixture):
    def test_failed_match_price_above_the_order_and_a_held_vendor_are_raised(self):
        found = {x["key"]: x for x in self.view()["exceptions"]}
        self.assertEqual(set(found), {"match_failed", "price_variance", "vendor_on_hold"})
        self.assertEqual(found["price_variance"]["amount"]["kobo"], 5 * 22_000)
        self.assertEqual(found["vendor_on_hold"]["detail"], "Prime Uniforms")

    def test_exceptions_and_bills_due_need_the_bills_key(self):
        d = self.view(reader=self.keys("procurement.purchase_order.view"))
        self.assertIsNone(d["exceptions"])
        self.assertIsNone(d["bills_due"])


class BillsAndContractsTests(_OverviewFixture):
    def test_late_bills_fall_in_the_first_late_bucket(self):
        buckets = {b["key"]: b["amount"]["kobo"] for b in self.view()["bills_due"]["items"]}
        self.assertEqual(buckets["1-30"], 5 * 22_000 + 2 * 3_000)
        self.assertEqual(buckets["current"], 0)

    def test_a_contract_ending_soon_shows_what_was_ordered_on_it(self):
        row = self.view()["contracts_ending"][0]
        self.assertEqual((row["title"], row["days"], row["ordered"]["kobo"]), ("Cleaning services", 20, 100_000))

    def test_a_branch_reader_does_not_see_every_branchs_orders_on_a_contract(self):
        row = self.view(branch=self.lekki)["contracts_ending"][0]
        self.assertIsNone(row["ordered"])


class SpendTests(_OverviewFixture):
    def test_committed_and_spent_are_read_by_month(self):
        chart = self.view()["committed_vs_spent"]
        self.assertEqual((chart["labels"][0], chart["current"]), ("Jan", 0))
        self.assertEqual(chart["committed"][0], 100_000 + 100_000 + 20_000 + 6_000)
        self.assertEqual(chart["spent"][0], 110_000 + 6_000)

    def test_top_vendors_and_categories_read_the_window(self):
        d = self.view(window="month")
        self.assertEqual(d["window"]["key"], "month")
        self.assertEqual(d["top_vendors"][0]["name"], "Acme Supplies")
        self.assertEqual(d["kpis"]["spend"]["value"]["kobo"], 116_000)


class EndpointTests(_OverviewFixture):
    def test_the_endpoint_accepts_a_window(self):
        client = self.client_for(self.multi_tenant, "proc-head@t.com")
        self.grant(client.test_user, "procurement.analytics.view", tenant=self.multi_tenant, role_key="role-head")
        response = client.get(f"/v1/procurement/reports/dashboard/?entity={self.multi.entity.code}&window=year")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["data"]["window"]["key"], "year")
