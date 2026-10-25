"""Receipt and return values follow their Field Access switches on API reads and writes."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import GoodsReceivedNote, GoodsReceivedNoteLine, GoodsReturn, GoodsReturnLine
from .tests_vendor_field_access import _VendorAccessFixture


class GoodsReceiptFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.goods_receipt")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for key in ("procurement.goods_receipt.view",
                        "procurement.goods_receipt.update",
                        "procurement.goods_receipt.reverse"):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)
        for books, branch in ((cls.multi, cls.lekki),
                              (cls.flat, cls.flat_tenant.branches.first())):
            books.receipt = GoodsReceivedNote.objects.create(
                entity=books.entity, vendor=books.vendor, branch=branch,
                received_date=datetime.date(2026, 1, 20), reference="DN-20",
                narration="Paper delivery", total_value=84000,
            )
            books.line = GoodsReceivedNoteLine.objects.create(
                grn=books.receipt, line_no=1, description="Paper",
                expense_account=cls.acc(books.entity, "5300"),
                accepted_qty=2, expected_qty=2, unit_price=42000,
                value_amount=84000,
            )
            goods_return = GoodsReturn.objects.create(
                entity=books.entity, vendor=books.vendor, branch=branch,
                grn=books.receipt, return_date=datetime.date(2026, 1, 21),
                reason="Damaged paper", total_value=42000,
            )
            GoodsReturnLine.objects.create(
                goods_return=goods_return, grn_line=books.line, line_no=1,
                quantity=1, value_amount=42000,
            )

    def test_default_read_then_hidden_receipt_and_return_fields(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(tenant=books.entity.code):
                path = f"goods-receipts/{books.receipt.pk}/"
                opened = self.call(user, path, books=books)
                self.assertEqual(opened.status_code, 200, opened.data)
                row = opened.data["data"]
                self.assertEqual(row["reference"], "DN-20")
                self.assertEqual(row["total_value"], 84000)
                self.assertEqual(row["returns"][0]["reason"], "Damaged paper")
                for field in ("reference", "total_value", "return_reason", "return_value"):
                    set_field_access(role, f"procurement.goods_receipt.{field}",
                                     read=False, write=False)
                detail = self.call(user, path, books=books)
                listed = self.call(user, "goods-receipts/", books=books)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertEqual(listed.status_code, 200, listed.data)
                for row in (detail.data["data"], listed.data["data"][0]):
                    self.assertNotIn("reference", row)
                    self.assertNotIn("total_value", row)
                    self.assertNotIn("total_value_naira", row)
                returned = detail.data["data"]["returns"][0]
                self.assertNotIn("reason", returned)
                self.assertNotIn("total_value", returned)

    def test_read_only_receipt_and_return_reject_writes(self):
        set_field_access(self.officer_role, "procurement.goods_receipt.reference",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"goods-receipts/{self.multi.receipt.pk}/", "patch",
                             {"reference": "DN-21"})
        self.assertEqual(response.status_code, 403, response.data)
        set_field_access(self.officer_role, "procurement.goods_receipt.return_reason",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"goods-receipts/{self.multi.receipt.pk}/reverse/", "post",
                             {"reason": "Wrong goods"})
        self.assertEqual(response.status_code, 403, response.data)

    def test_receipt_price_hides_derived_line_values_in_receipt_and_return(self):
        set_field_access(self.officer_role, "procurement.goods_receipt.receipt_unit_price",
                         read=False, write=False)
        response = self.call(self.officer_user,
                             f"goods-receipts/{self.multi.receipt.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        receipt = response.data["data"]
        self.assertNotIn("unit_price", receipt["lines"][0])
        self.assertNotIn("value_amount", receipt["lines"][0])
        self.assertNotIn("value_amount", receipt["returns"][0]["lines"][0])
