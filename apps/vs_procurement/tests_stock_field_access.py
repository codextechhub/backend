"""Stock values stay under the same switches on masters, balances and actions."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_rbac.field_evaluator import get_acting_field_access

from .models import StockItem, StockLocation
from .stock import receive_stock
from .tests_vendor_field_access import _VendorAccessFixture


class StockFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.stock")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for key in ("procurement.stock.view", "procurement.stock.create",
                        "procurement.stock.update", "procurement.stock.adjust"):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)
        for books, branch in ((cls.multi, cls.lekki),
                              (cls.flat, cls.flat_tenant.branches.first())):
            books.store = StockLocation.objects.create(
                entity=books.entity, branch=branch, code="MAIN", name="Main Store",
                is_default=True,
            )
            books.item = StockItem.objects.create(
                entity=books.entity, code="PAPER", name="Paper", description="A4",
                unit_of_measure="ream", inventory_account=cls.acc(books.entity, "1400"),
                default_expense_account=cls.acc(books.entity, "5300"),
                reorder_level=10, reorder_qty=20,
            )
            receive_stock(
                books.item, quantity=2, value=8400000,
                movement_date=datetime.date(2026, 1, 20), location=books.store,
            )

    def test_open_defaults_and_hidden_item_values_on_each_branch_shape(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(books=books.entity.code):
                detail_path = f"stock-items/{books.item.pk}/"
                opened = self.call(user, detail_path, books=books)
                self.assertEqual(opened.status_code, 200, opened.data)
                self.assertEqual(opened.data["data"]["description"], "A4")
                for field in ("description", "stock_value", "unit_cost", "reorder_level"):
                    set_field_access(role, f"procurement.stock.{field}",
                                     read=False, write=False)
                self.assertFalse(get_acting_field_access(user, tenant=user.tenant).can_read(
                    "procurement.stock.description"))
                detail = self.call(user, detail_path, books=books)
                listed = self.call(user, "stock-items/", books=books)
                summary = self.call(user, "stock-items/summary/", books=books)
                for response in (detail, listed, summary):
                    self.assertEqual(response.status_code, 200, response.data)
                for row in (detail.data["data"], listed.data["data"][0]):
                    for field in ("description", "stock_value", "stock_value_naira",
                                  "unit_cost", "unit_cost_naira", "reorder_level",
                                  "needs_reorder"):
                        self.assertNotIn(field, row)
                self.assertNotIn("total_value", summary.data["data"])
                self.assertEqual(self.call(user, "stock-items/?needs_reorder=true",
                                           books=books).status_code, 403)
                self.assertEqual(self.call(user, detail_path, "patch",
                                           {"description": "Legal"}, books=books).status_code, 403)

    def test_adjust_cost_is_a_separate_write_switch(self):
        set_field_access(self.officer_role, "procurement.stock.adjust_unit_cost",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"stock-items/{self.multi.item.pk}/adjust/", "post",
                             {"quantity_delta": "1", "unit_cost": 10000,
                              "location": self.multi.store.pk})
        self.assertEqual(response.status_code, 403, response.data)

    def test_movement_and_balance_values_are_hidden_on_every_read_path(self):
        for field in ("stock_value", "unit_cost", "movement_value", "balance_value"):
            set_field_access(self.officer_role, f"procurement.stock.{field}",
                             read=False, write=False)
        for path in ("stock-balances/", "stock-movements/",
                     f"stock-items/{self.multi.item.pk}/"):
            response = self.call(self.officer_user, path)
            self.assertEqual(response.status_code, 200, response.data)
            payload = response.data["data"]
            rows = payload if isinstance(payload, list) else payload.get("movements", [])
            for row in rows:
                for key in ("stock_value", "unit_cost", "value_amount", "value_amount_naira",
                            "balance_value", "balance_value_naira"):
                    self.assertNotIn(key, row)

    def test_item_account_write_is_denied(self):
        set_field_access(self.officer_role, "procurement.stock.inventory_account",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"stock-items/{self.multi.item.pk}/", "patch",
                             {"inventory_account": "1400"})
        self.assertEqual(response.status_code, 403, response.data)
