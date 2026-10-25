"""Credit note headers and derived amounts follow their own switches."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import VendorCreditNote, VendorInvoice
from .tests_vendor_field_access import _VendorAccessFixture


class CreditNoteFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.vendor_credit_note")
        for key in ("procurement.vendor_credit_note.view",
                    "procurement.vendor_credit_note.update"):
            cls.grant(cls.officer_user, key, tenant=cls.multi_tenant,
                      role_key="officer", branch=None)
        cls.bill = VendorInvoice.objects.create(
            entity=cls.multi.entity, vendor=cls.multi.vendor,
            invoice_date=datetime.date(2026, 1, 20),
        )
        cls.note = VendorCreditNote.objects.create(
            entity=cls.multi.entity, vendor=cls.multi.vendor, vendor_invoice=cls.bill,
            branch=cls.lekki, note_date=datetime.date(2026, 1, 21),
            reason="Damaged paper", subtotal=10000, total=10000,
        )

    def test_default_read_and_hidden_values_on_list_and_detail(self):
        path = f"vendor-credit-notes/{self.note.pk}/"
        opened = self.call(self.officer_user, path)
        self.assertEqual(opened.status_code, 200, opened.data)
        self.assertEqual(opened.data["data"]["reason"], "Damaged paper")
        for field in ("reason", "amounts", "settlement", "vendor_invoice"):
            set_field_access(self.officer_role, f"procurement.vendor_credit_note.{field}",
                             read=False, write=False)
        detail = self.call(self.officer_user, path)
        listed = self.call(self.officer_user, "vendor-credit-notes/")
        for response in (detail, listed):
            self.assertEqual(response.status_code, 200, response.data)
        for row in (detail.data["data"], listed.data["data"][0]):
            for key in ("reason", "subtotal", "tax_total", "total",
                        "allocated_amount", "advance_remaining",
                        "vendor_invoice_id", "vendor_invoice_number"):
                self.assertNotIn(key, row)
        self.assertEqual(self.call(self.officer_user,
                                   f"vendor-credit-notes/?vendor_invoice={self.bill.pk}").status_code, 403)
        self.assertEqual(self.call(self.officer_user, path, "patch",
                                   {"reason": "Changed"}).status_code, 403)
