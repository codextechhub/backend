"""Payment amounts, tax and bank choices follow their Field Access switches."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import VendorPayment
from .tests_vendor_field_access import _VendorAccessFixture


class VendorPaymentFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.vendor_payment")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for key in ("procurement.vendor_payment.view",
                        "procurement.vendor_payment.update",
                        "procurement.vendor_payment.allocate"):
                cls.grant(user, key, tenant=tenant, role_key="officer", branch=None)
        for books, branch in ((cls.multi, cls.lekki),
                              (cls.flat, cls.flat_tenant.branches.first())):
            books.payment = VendorPayment.objects.create(
                entity=books.entity, vendor=books.vendor, branch=branch,
                payment_date=datetime.date(2026, 1, 20), gross_amount=100000,
                wht_amount=5000, net_amount=95000, allocated_amount=0,
                reference="TRF-20", narration="Paper supplier",
            )

    def test_default_open_then_hidden_on_list_and_detail(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(tenant=books.entity.code):
                path = f"vendor-payments/{books.payment.pk}/"
                opened = self.call(user, path, books=books)
                self.assertEqual(opened.status_code, 200, opened.data)
                self.assertEqual(opened.data["data"]["gross_amount"], 100000)
                self.assertEqual(opened.data["data"]["wht_amount"], 5000)
                for field in ("gross_amount", "net_amount", "wht_amount",
                              "reference", "narration"):
                    set_field_access(role, f"procurement.vendor_payment.{field}",
                                     read=False, write=False)
                detail = self.call(user, path, books=books)
                listed = self.call(user, "vendor-payments/", books=books)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertEqual(listed.status_code, 200, listed.data)
                for row in (detail.data["data"], listed.data["data"][0]):
                    for name in ("gross_amount", "net_amount", "net_naira",
                                 "wht_amount", "wht_source", "reference", "narration"):
                        self.assertNotIn(name, row)
                self.assertNotIn("posting_lines", detail.data["data"])
                self.assertNotIn("activity", detail.data["data"])

    def test_read_only_bank_and_tax_inputs_are_rejected(self):
        set_field_access(self.officer_role,
                         "procurement.vendor_payment.paid_from_account",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"vendor-payments/{self.multi.payment.pk}/", "patch",
                             {"bank_account": 99})
        self.assertEqual(response.status_code, 403, response.data)
        set_field_access(self.officer_role, "procurement.vendor_payment.wht_amount",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"vendor-payments/{self.multi.payment.pk}/", "patch",
                             {"wht_amount": 0})
        self.assertEqual(response.status_code, 403, response.data)

    def test_auto_allocate_needs_allocation_write(self):
        set_field_access(self.officer_role, "procurement.vendor_payment.allocations",
                         read=True, write=False)
        response = self.call(self.officer_user,
                             f"vendor-payments/{self.multi.payment.pk}/allocate/", "post",
                             {"auto_allocate": True})
        self.assertEqual(response.status_code, 403, response.data)
