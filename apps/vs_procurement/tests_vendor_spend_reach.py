"""Vendor screens add up the bills the reader can open, not the school's.

The overview school buys from Acme Supplies at both branches, and Acme is a
school-wide vendor, so every reader can open it. By 31 January 2026:

* Ikeja has been billed 110,000 kobo for desks, and 6,000 by Prime Uniforms;
* Lekki has been billed 40,000 for the chairs it received, and still has two
  orders (chairs and lamps) waiting on deliveries.

The vendor master is shared; the spend on it is not. The Ikeja storekeeper's
vendor header, category cards and Acme drawer read Ikeja's 110,000, the Lekki
storekeeper's read 40,000, and only a reader covering the whole school reads
150,000. The open-order count on the vendor list follows the same rule.
"""
from __future__ import annotations

from unittest import mock

from .models import GoodsReceivedNote, Vendor, VendorCategory
from .tests_dashboard_overview import AS_OF, _OverviewFixture

KEYS = ("procurement.report.view", "procurement.analytics.view", "procurement.vendor.view")


class _VendorSpendFixture(_OverviewFixture):
    """The overview school with the chairs billed, a vendor category and three readers.

    The readers' users and grants are built once per class; their API clients and
    the frozen vendor-screen clock are made per test.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        e = cls.multi.entity
        grn = GoodsReceivedNote.objects.get(purchase_order=cls.chairs)
        cls.bill(cls.chairs, grn, 4)
        cls.stationery = VendorCategory.objects.create(entity=e, code="STAT", name="Stationery")
        Vendor.objects.filter(pk=cls.multi.vendor.pk).update(category=cls.stationery)

        cls.ikeja_reader_user = cls.reader("ikeja.store@t.com", "spend-ikeja", cls.ikeja)
        cls.lekki_reader_user = cls.reader("lekki.store@t.com", "spend-lekki", cls.lekki)
        cls.head_user = cls.reader("head.store@t.com", "spend-head", None)

    def setUp(self):
        super().setUp()
        self.ikeja_reader = self.client_as(self.ikeja_reader_user)
        self.lekki_reader = self.client_as(self.lekki_reader_user)
        self.head = self.client_as(self.head_user)

        clock = mock.patch("vs_procurement.views.vendors.tenant_today", return_value=AS_OF)
        clock.start()
        self.addCleanup(clock.stop)

    @classmethod
    def reader(cls, email, role_key, branch):
        """A user of the school granted every spend key at ``branch``."""
        user = cls.user_for(cls.multi_tenant, email)
        for key in KEYS:
            cls.grant(user, key, tenant=cls.multi_tenant,
                      role_key=role_key, branch=branch)
        return user

    def fetch(self, client, path):
        response = client.get(f"/v1/procurement/{path}?entity={self.multi.entity.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]


class VendorSummarySpendTests(_VendorSpendFixture):
    def test_a_branch_reader_sees_their_branchs_spend(self):
        self.assertEqual(self.fetch(self.ikeja_reader, "vendors/summary/")["total_spend_ytd"],
                         110_000 + 6_000)
        self.assertEqual(self.fetch(self.lekki_reader, "vendors/summary/")["total_spend_ytd"],
                         40_000)

    def test_a_whole_school_reader_sees_the_schools_spend(self):
        self.assertEqual(self.fetch(self.head, "vendors/summary/")["total_spend_ytd"],
                         110_000 + 6_000 + 40_000)


class VendorInsightsSpendTests(_VendorSpendFixture):
    def insights(self, client):
        return self.fetch(client, f"vendors/{self.multi.vendor.pk}/insights/")

    def test_a_branch_readers_drawer_is_their_branchs_business_with_the_vendor(self):
        ikeja = self.insights(self.ikeja_reader)
        lekki = self.insights(self.lekki_reader)

        self.assertEqual((ikeja["spend_ytd"], ikeja["invoice_count"]), (110_000, 1))
        self.assertEqual(ikeja["po_count"], 1)
        self.assertEqual((lekki["spend_ytd"], lekki["invoice_count"]), (40_000, 1))
        self.assertEqual(lekki["po_count"], 2)

    def test_a_whole_school_reader_sees_all_of_it(self):
        head = self.insights(self.head)

        self.assertEqual((head["spend_ytd"], head["invoice_count"]), (150_000, 2))
        self.assertEqual(head["po_count"], 3)


class CategorySpendTests(_VendorSpendFixture):
    def stationery_row(self, client):
        rows = self.fetch(client, "categories/insights/")
        return next(r for r in rows if r["category_id"] == self.stationery.pk)

    def test_category_cards_count_only_the_readers_bills(self):
        self.assertEqual(self.stationery_row(self.ikeja_reader)["spend_ytd"], 110_000)
        self.assertEqual(self.stationery_row(self.lekki_reader)["spend_ytd"], 40_000)
        self.assertEqual(self.stationery_row(self.head)["spend_ytd"], 150_000)


class VendorListOpenOrderTests(_VendorSpendFixture):
    def open_orders(self, client):
        rows = self.fetch(client, "vendors/")
        return next(r for r in rows if r["code"] == "ACME")["active_po_count"]

    def test_the_open_order_count_is_the_readers_branches_orders(self):
        self.assertEqual(self.open_orders(self.ikeja_reader), 0)
        self.assertEqual(self.open_orders(self.lekki_reader), 2)
        self.assertEqual(self.open_orders(self.head), 2)
