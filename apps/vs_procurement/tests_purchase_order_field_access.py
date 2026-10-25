"""Purchase order terms, totals and linked documents follow Field Access."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import PurchaseOrder, PurchaseOrderLine
from .tests_vendor_field_access import _VendorAccessFixture


class PurchaseOrderFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.purchase_order")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for key in ("procurement.purchase_order.view", "procurement.purchase_order.update"):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)
        for books, branch in ((cls.multi, cls.lekki),
                              (cls.flat, cls.flat_tenant.branches.first())):
            books.order = PurchaseOrder.objects.create(
                entity=books.entity, vendor=books.vendor, branch=branch,
                order_date=datetime.date(2026, 1, 20),
                expected_date=datetime.date(2026, 2, 20),
                delivery_address="Library store", payment_terms="30 days",
                subtotal=100000, total=100000,
            )
            PurchaseOrderLine.objects.create(
                purchase_order=books.order, line_no=1, description="Paper",
                expense_account=cls.acc(books.entity, "5300"), quantity=2,
                unit_price=50000, net_amount=100000,
            )

    def test_open_defaults_and_hidden_list_detail_summary(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(tenant=books.entity.code):
                path = f"purchase-orders/{books.order.pk}/"
                opened = self.call(user, path, books=books)
                self.assertEqual(opened.status_code, 200, opened.data)
                self.assertEqual(opened.data["data"]["total"], 100000)
                self.assertEqual(opened.data["data"]["lines"][0]["unit_price"], 50000)
                for field in ("total", "payment_terms", "unit_price", "email_history"):
                    set_field_access(role, f"procurement.purchase_order.{field}",
                                     read=False, write=False)
                detail = self.call(user, path, books=books)
                listed = self.call(user, "purchase-orders/", books=books)
                summary = self.call(user, "purchase-orders/summary/", books=books)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertEqual(listed.status_code, 200, listed.data)
                self.assertEqual(summary.status_code, 200, summary.data)
                for row in (detail.data["data"], listed.data["data"][0]):
                    for name in ("total", "subtotal", "tax_total", "total_naira",
                                 "payment_terms"):
                        self.assertNotIn(name, row)
                self.assertNotIn("email_deliveries", detail.data["data"])
                line = detail.data["data"]["lines"][0]
                for name in ("unit_price", "net_amount", "tax_amount"):
                    self.assertNotIn(name, line)
                self.assertNotIn("amount", summary.data["data"]["open"])
                self.assertNotIn("amount", summary.data["data"]["po_value_mtd"])

    def test_read_only_term_rejects_patch(self):
        set_field_access(self.officer_role, "procurement.purchase_order.payment_terms",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"purchase-orders/{self.multi.order.pk}/", "patch",
                             {"payment_terms": "45 days"})
        self.assertEqual(response.status_code, 403, response.data)
