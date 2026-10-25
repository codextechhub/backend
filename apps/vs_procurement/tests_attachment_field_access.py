"""Bill and payment evidence each has one independent Field Access switch."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import VendorInvoice, VendorPayment
from .tests_vendor_field_access import _VendorAccessFixture


class DocumentAttachmentFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.vendor_invoice", "procurement.vendor_payment")
        for key in ("procurement.vendor_invoice.view", "procurement.vendor_invoice.attach",
                    "procurement.vendor_payment.view", "procurement.vendor_payment.attach"):
            cls.grant(cls.officer_user, key, tenant=cls.multi_tenant,
                      role_key="officer", branch=None)
        cls.bill = VendorInvoice.objects.create(
            entity=cls.multi.entity, vendor=cls.multi.vendor,
            invoice_date=datetime.date(2026, 1, 20),
        )
        cls.payment = VendorPayment.objects.create(
            entity=cls.multi.entity, vendor=cls.multi.vendor,
            payment_date=datetime.date(2026, 1, 20),
            gross_amount=10000, net_amount=10000,
        )

    def test_bill_and_payment_switches_are_independent_on_direct_routes(self):
        set_field_access(self.officer_role, "procurement.vendor_invoice.attachments",
                         read=False, write=False)
        bill_path = f"vendor-invoices/{self.bill.pk}/attachments/"
        payment_path = f"vendor-payments/{self.payment.pk}/attachments/"
        self.assertEqual(self.call(self.officer_user, bill_path).status_code, 403)
        self.assertEqual(self.call(self.officer_user, bill_path, "post").status_code, 403)
        self.assertEqual(self.call(self.officer_user, payment_path).status_code, 200)

    def test_payment_write_can_be_closed_while_attachments_remain_readable(self):
        set_field_access(self.officer_role, "procurement.vendor_payment.attachments",
                         read=True, write=False)
        path = f"vendor-payments/{self.payment.pk}/attachments/"
        self.assertEqual(self.call(self.officer_user, path).status_code, 200)
        self.assertEqual(self.call(self.officer_user, path, "post").status_code, 403)
