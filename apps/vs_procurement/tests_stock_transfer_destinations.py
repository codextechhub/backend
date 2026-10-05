"""A storekeeper picks another branch's store to send goods to, and learns only its name.

Corona runs Ikeja, Lekki and Yaba, each with its own store, and Ikeja also keeps
a lab store. Ikeja's storekeeper, who reads only Ikeja's stores, sends textbooks
to Lekki: the transfer accepts any live store of the books as its destination,
so the form lists every one of them by code and name, with nothing about what
any store holds. A retired store is not offered, and another school's stores
never appear.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from core.test_utils import TenantAPIClient
from vs_finance.models import Account
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_procurement.models import StockItem, StockLocation
from vs_procurement.stock import receive_stock


class StockTransferDestinationTests(_FinanceBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_store = cls.store(cls.books, "IKJ", "Ikeja store", cls.ikeja)
        cls.ikeja_lab = cls.store(cls.books, "IKJ-LAB", "Ikeja lab store", cls.ikeja)
        cls.lekki_store = cls.store(cls.books, "LEK", "Lekki store", cls.lekki)
        cls.yaba_store = cls.store(cls.books, "YAB", "Yaba store", cls.yaba)
        cls.retired = cls.store(cls.books, "OLD", "Old annex", cls.lekki, is_active=False)
        cls.rival_store = cls.store(cls.rival_books, "RIV", "Rival store", cls.rival_branch)
        item = StockItem.objects.create(
            entity=cls.books, code="BOOK", name="Textbook",
            inventory_account=Account.objects.get(entity=cls.books, code="1400"),
        )
        receive_stock(item, quantity=Decimal(10), value=50_000, movement_date=datetime.date(2026, 1, 5),
                      location=cls.lekki_store)
        cls.storekeeper = cls.grant(
            cls.user_for(cls.tenant, "ikeja-keeper@corona.test"), "procurement.stock.issue",
            tenant=cls.tenant, role_key="ikeja-keeper", branch=cls.ikeja,
        )
        cls.viewer = cls.grant(
            cls.user_for(cls.tenant, "ikeja-viewer@corona.test"), "procurement.stock.view",
            tenant=cls.tenant, role_key="ikeja-viewer", branch=cls.ikeja,
        )

    @classmethod
    def store(cls, books, code, name, branch, *, is_active=True):
        return StockLocation.objects.create(
            entity=books, code=code, name=name, branch=branch, is_active=is_active,
        )

    def destinations(self, user, query="", books=None):
        books = books or self.books
        return TenantAPIClient(user=user).get(
            f"/v1/procurement/stock-locations/transfer-destinations/?entity={books.code}{query}")

    def test_an_ikeja_storekeeper_sees_every_live_store_by_name_only(self):
        response = self.destinations(self.storekeeper)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [
            {"id": self.ikeja_store.pk, "code": "IKJ", "name": "Ikeja store",
             "branch_id": self.ikeja.pk, "branch_name": "Ikeja Branch"},
            {"id": self.ikeja_lab.pk, "code": "IKJ-LAB", "name": "Ikeja lab store",
             "branch_id": self.ikeja.pk, "branch_name": "Ikeja Branch"},
            {"id": self.lekki_store.pk, "code": "LEK", "name": "Lekki store",
             "branch_id": self.lekki.pk, "branch_name": "Lekki Branch"},
            {"id": self.yaba_store.pk, "code": "YAB", "name": "Yaba store",
             "branch_id": self.yaba.pk, "branch_name": "Yaba Branch"},
        ])

    def test_the_stores_own_list_still_shows_ikeja_only_its_own(self):
        response = TenantAPIClient(user=self.viewer).get(
            f"/v1/procurement/stock-locations/?entity={self.books.code}")

        self.assertEqual({row["code"] for row in response.data["data"]}, {"IKJ", "IKJ-LAB"})

    def test_search_matches_a_code_or_a_name(self):
        response = self.destinations(self.storekeeper, "&search=lekki")

        self.assertEqual([row["code"] for row in response.data["data"]], ["LEK"])

    def test_a_reader_who_cannot_transfer_is_refused(self):
        self.assertEqual(self.destinations(self.viewer).status_code, 403)

    def test_another_schools_books_are_not_reachable(self):
        response = self.destinations(self.storekeeper, books=self.rival_books)

        self.assertIn(response.status_code, (403, 404))

    def test_a_one_branch_school_lists_its_own_stores(self):
        main = self.store(self.solo_books, "MAIN", "Main store", self.solo_main)
        keeper = self.grant(
            self.user_for(self.solo_tenant, "solo-keeper@solo.test"), "procurement.stock.issue",
            tenant=self.solo_tenant, role_key="solo-keeper",
        )

        response = self.destinations(keeper, books=self.solo_books)

        self.assertEqual([row["id"] for row in response.data["data"]], [main.pk])
