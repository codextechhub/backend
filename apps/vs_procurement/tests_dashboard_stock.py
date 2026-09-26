"""The Stock & receiving tab, stock issued to a cost centre, and the restock draft.

The two-branch school from the overview tests keeps paper in a school-wide
central store and in a Lekki store, read as at 31 January 2026. The central
store received 100 reams on 2 January (reorder level 40, reorder quantity 100)
and issued 70 to Administration by the 30th; Lekki received 10 and issued none.

A Lekki storekeeper reads the central store and Lekki's own, never Ikeja's. The
restock draft is built from the server's own reading of low stock, is left as a
draft, and needs both the requisition key and access to stock.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from vs_finance.models import CostCenter, JournalLine

from .dashboard_stock import low_stock, stock_view
from .models import PurchaseRequisition, StockItem, StockLocation, StockMovement
from .stock import issue_stock, receive_stock
from .tests_dashboard_overview import AS_OF, _OverviewFixture


class _StockFixture(_OverviewFixture):
    def setUp(self):
        super().setUp()
        e = self.multi.entity
        self.central = StockLocation.objects.filter(entity=e, is_default=True).first() or StockLocation.objects.create(
            entity=e, code="CENTRAL", name="Central store", is_default=True)
        self.lekki_store = StockLocation.objects.create(entity=e, code="LEK", name="Lekki store", branch=self.lekki)
        self.admin = CostCenter.objects.create(entity=e, code="ADMIN", name="Administration")
        self.paper = StockItem.objects.create(
            entity=e, code="PAPER", name="A4 paper", unit_of_measure="ream", reorder_level=40, reorder_qty=100,
            inventory_account=self.acc(e, "1400"), default_expense_account=self.acc(e, "5300"),
        )
        receive_stock(self.paper, quantity=100, value=500_000, movement_date=datetime.date(2026, 1, 2),
                      location=self.central)
        receive_stock(self.paper, quantity=10, value=50_000, movement_date=datetime.date(2026, 1, 2),
                      location=self.lekki_store)
        for day, qty in ((10, 30), (20, 20), (30, 20)):
            issue_stock(self.paper, quantity=Decimal(qty), movement_date=datetime.date(2026, 1, day),
                        location=self.central, cost_center=self.admin)

    def stores(self, branch=None):
        from vs_rbac.scoping import BranchScope

        from .views.base import _BranchScope

        return _BranchScope(BranchScope(frozenset({branch.id}), include_shared=True), {}) if branch else None

    def tab(self, reader=None, branch=None):
        from vs_finance.dashboard import EVERY_BLOCK

        return stock_view(self.multi.entity, as_of=AS_OF, reader=reader or EVERY_BLOCK,
                          store_scope=self.stores(branch), window="month")


class LowStockTests(_StockFixture):
    def test_an_item_at_or_below_its_reorder_level_is_low_with_days_left(self):
        # 40 reams on hand across both stores; 70 issued over the 30 days since it first moved.
        row = low_stock(self.multi.entity, AS_OF)[0]
        self.assertEqual((row.item.code, row.on_hand), ("PAPER", Decimal(40)))
        self.assertEqual(row.days_left, int(Decimal(40) / (Decimal(70) / 30)))
        self.assertEqual(row.suggested, Decimal(100))

    def test_a_lekki_storekeeper_reads_the_central_store_and_their_own(self):
        issue_stock(self.paper, quantity=Decimal(10), movement_date=datetime.date(2026, 1, 30), location=self.central)
        ikeja_store = StockLocation.objects.create(entity=self.multi.entity, code="IKJ", name="Ikeja store",
                                                   branch=self.ikeja)
        receive_stock(self.paper, quantity=500, value=2_500_000, movement_date=datetime.date(2026, 1, 3),
                      location=ikeja_store)
        # Across every store there are 530 reams; Lekki reads 30 (central 20, Lekki 10) and sees it low.
        self.assertEqual(low_stock(self.multi.entity, AS_OF), [])
        lekki = low_stock(self.multi.entity, AS_OF, self.stores(self.lekki))
        self.assertEqual(lekki[0].on_hand, Decimal(30))

    def test_the_tab_counts_what_is_low_and_what_went_to_whom(self):
        d = self.tab()
        self.assertEqual(d["position"]["below_reorder"], 1)
        self.assertEqual(d["issued"]["items"][0]["name"], "Administration")
        self.assertEqual(d["running_low"][0]["name"], "A4 paper")

    def test_stock_blocks_need_the_stock_key(self):
        d = self.tab(reader=self.keys("procurement.purchase_order.view"))
        for block in ("position", "running_low", "by_store", "issued", "movements", "turns", "adjustments"):
            self.assertIsNone(d[block], block)
        self.assertIsNotNone(d["expected"])


class IssueCostCentreTests(_StockFixture):
    def test_the_cost_centre_is_kept_on_the_movement_and_the_expense_line(self):
        movement = StockMovement.objects.filter(stock_item=self.paper, movement_type="ISSUE").first()
        self.assertEqual(movement.cost_center, self.admin)
        line = JournalLine.objects.get(entry=movement.journal, debit__gt=0)
        self.assertEqual(line.cost_center, self.admin)

    def test_the_issue_endpoint_refuses_a_cost_centre_of_other_books(self):
        other = CostCenter.objects.create(entity=self.flat.entity, code="ELSE", name="Elsewhere")
        client = self.client_for(self.multi_tenant, "keeper@t.com")
        self.grant(client.test_user, "procurement.stock.issue", tenant=self.multi_tenant, role_key="role-keeper")
        response = client.post(
            f"/v1/procurement/stock-items/{self.paper.pk}/issue/?entity={self.multi.entity.code}",
            {"quantity": "1", "location": self.central.pk, "cost_center": other.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_storekeeper_may_read_the_cost_centres_to_pick_one(self):
        client = self.client_for(self.multi_tenant, "keeper2@t.com")
        self.grant(client.test_user, "procurement.stock.issue", tenant=self.multi_tenant, role_key="role-keeper2")
        response = client.get(f"/v1/finance/cost-centers/?entity={self.multi.entity.code}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))


class RestockDraftTests(_StockFixture):
    def client_with(self, email, *keys):
        client = self.client_for(self.multi_tenant, email)
        for key in keys:
            self.grant(client.test_user, key, tenant=self.multi_tenant, role_key=f"role-{email}")
        return client

    def post(self, client, body=None):
        return client.post(f"/v1/procurement/stock-items/restock-requisition/?entity={self.multi.entity.code}",
                           body or {}, format="json")

    def test_it_drafts_one_line_per_low_item_at_the_suggested_quantity(self):
        response = self.post(self.client_with("buyer@t.com", "procurement.requisition.create", "procurement.stock.view"))
        self.assertEqual(response.status_code, 201, response.data)
        req = PurchaseRequisition.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(req.status, "DRAFT")
        line = req.lines.get()
        self.assertEqual((line.description, line.quantity), ("A4 paper", Decimal(100)))
        self.assertEqual(line.estimated_unit_price, 5_000)  # 200,000 on hand for 40 reams.

    def test_it_needs_access_to_stock(self):
        response = self.post(self.client_with("buyer2@t.com", "procurement.requisition.create"))
        self.assertEqual(response.status_code, 403)

    def test_nothing_low_means_nothing_drafted(self):
        StockItem.objects.filter(pk=self.paper.pk).update(reorder_level=0)
        response = self.post(self.client_with("buyer3@t.com", "procurement.requisition.create",
                                              "procurement.stock.view"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PurchaseRequisition.objects.filter(title__startswith="Restock").exists())


class StockEndpointTests(_StockFixture):
    def test_the_endpoint_opens_to_a_stock_reader(self):
        client = self.client_for(self.multi_tenant, "store-head@t.com")
        self.grant(client.test_user, "procurement.stock.view", tenant=self.multi_tenant, role_key="role-store")
        response = client.get(f"/v1/procurement/reports/dashboard/stock/?entity={self.multi.entity.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["data"]["position"]["below_reorder"], 1)
