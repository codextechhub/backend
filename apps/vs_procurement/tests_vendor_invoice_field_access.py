"""Bill headers, amounts and nested line prices follow their Field Access switches."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import VendorInvoice, VendorInvoiceLine
from .tests_vendor_field_access import _VendorAccessFixture


class VendorInvoiceFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.vendor_invoice")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for key in ("procurement.vendor_invoice.view",
                        "procurement.vendor_invoice.update",
                        "procurement.vendor_invoice.import_opening"):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)
        for books, branch in ((cls.multi, cls.lekki),
                              (cls.flat, cls.flat_tenant.branches.first())):
            books.bill = VendorInvoice.objects.create(
                entity=books.entity, vendor=books.vendor, branch=branch,
                invoice_date=datetime.date(2026, 1, 20),
                due_date=datetime.date(2026, 2, 20), vendor_reference="ADE-20",
                narration="Printer paper", subtotal=100000, total=100000,
            )
            VendorInvoiceLine.objects.create(
                vendor_invoice=books.bill, line_no=1, description="Paper",
                expense_account=cls.acc(books.entity, "5300"),
                quantity=2, unit_price=50000, net_amount=100000,
            )

    def test_default_open_then_hidden_on_list_detail_and_reference_check(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(tenant=books.entity.code):
                path = f"vendor-invoices/{books.bill.pk}/"
                opened = self.call(user, path, books=books)
                self.assertEqual(opened.status_code, 200, opened.data)
                self.assertEqual(opened.data["data"]["total"], 100000)
                self.assertEqual(opened.data["data"]["lines"][0]["unit_price"], 50000)
                for field in ("total", "vendor_reference", "bill_unit_price"):
                    set_field_access(role, f"procurement.vendor_invoice.{field}",
                                     read=False, write=False)
                detail = self.call(user, path, books=books)
                listed = self.call(user, "vendor-invoices/", books=books)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertEqual(listed.status_code, 200, listed.data)
                for row in (detail.data["data"], listed.data["data"][0]):
                    for name in ("subtotal", "tax_total", "total", "total_naira",
                                 "vendor_reference"):
                        self.assertNotIn(name, row)
                line = detail.data["data"]["lines"][0]
                self.assertNotIn("unit_price", line)
                self.assertNotIn("net_amount", line)
                self.assertNotIn("tax_amount", line)
                self.assertNotIn("posting_lines", detail.data["data"])
                self.assertEqual(self.call(user,
                                           "vendor-invoices/reference-check/?vendor=ACME&reference=ADE-20",
                                           books=books).status_code, 403)

    def test_read_only_header_and_line_price_reject_writes(self):
        set_field_access(self.officer_role,
                         "procurement.vendor_invoice.vendor_reference",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"vendor-invoices/{self.multi.bill.pk}/", "patch",
                             {"vendor_reference": "ADE-21"})
        self.assertEqual(response.status_code, 403, response.data)
        set_field_access(self.officer_role,
                         "procurement.vendor_invoice.bill_unit_price",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"vendor-invoices/{self.multi.bill.pk}/", "patch",
                             {"lines": [{"unit_price": 90000}]})
        self.assertEqual(response.status_code, 403, response.data)
