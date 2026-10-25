"""Catalogue defaults, contract terms and assessments follow their field switches."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import CatalogItem, VendorAssessment, VendorContract
from .tests_vendor_field_access import _VendorAccessFixture


class ProcurementMasterFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for resource in ("catalog_item", "contract", "vendor_assessment"):
            install_declared_fields(f"procurement.{resource}")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for permission in ("procurement.catalog_item.view",
                               "procurement.catalog_item.update", "procurement.contract.view",
                               "procurement.contract.update", "procurement.analytics.view",
                               "procurement.vendor_assessment.create"):
                cls.grant(user, permission, tenant=tenant, role_key="officer", branch=None)
        for books in (cls.multi, cls.flat):
            books.item = CatalogItem.objects.create(
                entity=books.entity, code="PAPER", name="Paper", description="White reams",
                standard_unit_price=42000,
            )
            books.contract = VendorContract.objects.create(
                entity=books.entity, vendor=books.vendor, reference="C-1",
                title="Paper supply", contract_value=2500000,
                start_date=datetime.date(2026, 1, 1),
            )
            books.assessment = VendorAssessment.objects.create(
                entity=books.entity, vendor=books.vendor,
                assessment_date=datetime.date(2026, 1, 2),
                on_time_delivery=80, quality_acceptance=70,
                invoice_accuracy=90, responsiveness=80,
                notes="Private judgement",
            )

    def test_catalogue_hidden_and_read_only_fields_on_both_branch_shapes(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(tenant=books.entity.code):
                set_field_access(role, "procurement.catalog_item.standard_unit_price",
                                 read=False, write=False)
                detail = self.call(user, f"catalog-items/{books.item.pk}/", books=books)
                listed = self.call(user, "catalog-items/", books=books)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertEqual(listed.status_code, 200, listed.data)
                for row in (detail.data["data"], listed.data["data"][0]):
                    self.assertNotIn("standard_unit_price", row)
                    self.assertNotIn("standard_unit_price_naira", row)
                denied = self.call(user, f"catalog-items/{books.item.pk}/", "patch",
                                   {"standard_unit_price": 43000}, books=books)
                self.assertEqual(denied.status_code, 403, denied.data)

    def test_contract_term_and_value_are_separate(self):
        set_field_access(self.officer_role, "procurement.contract.contract_value",
                         read=False, write=False)
        detail = self.call(self.officer_user, f"contracts/{self.multi.contract.pk}/")
        listed = self.call(self.officer_user, "contracts/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertIn("start_date", detail.data["data"])
        self.assertNotIn("contract_value", detail.data["data"])
        self.assertNotIn("contract_value", listed.data["data"][0])
        denied = self.call(self.officer_user, f"contracts/{self.multi.contract.pk}/",
                           "patch", {"contract_value": 2600000})
        self.assertEqual(denied.status_code, 403, denied.data)

    def test_assessment_notes_start_sensitive_and_scores_can_be_closed(self):
        listed = self.call(self.officer_user, "vendor-assessments/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertNotIn("notes", listed.data["data"][0])
        set_field_access(self.officer_role, "procurement.vendor_assessment.scores",
                         read=False, write=False)
        listed = self.call(self.officer_user, "vendor-assessments/")
        row = listed.data["data"][0]
        for key in ("on_time_delivery", "quality_acceptance", "invoice_accuracy",
                    "responsiveness", "overall_score", "grade"):
            self.assertNotIn(key, row)

    def test_vendor_active_order_count_has_its_own_read_switch(self):
        install_declared_fields("procurement.vendor")
        open_row = self.call(self.officer_user, "vendors/")
        self.assertEqual(open_row.status_code, 200, open_row.data)
        self.assertIn("active_po_count", open_row.data["data"][0])
        set_field_access(self.officer_role, "procurement.vendor.active_po_count",
                         read=False, write=False)
        listed = self.call(self.officer_user, "vendors/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertNotIn("active_po_count", listed.data["data"][0])
