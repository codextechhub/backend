"""Sourcing documents apply Field Access to headers and nested prices."""

import datetime

from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import (PurchaseRequisition, PurchaseRequisitionLine,
                     RequestForQuotation, RfqLine, VendorQuotation,
                     VendorQuotationLine)
from .tests_vendor_field_access import _VendorAccessFixture


class SourcingFieldAccessTests(_VendorAccessFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for resource in ("requisition", "rfq", "quotation"):
            install_declared_fields(f"procurement.{resource}")
        for user, tenant in ((cls.officer_user, cls.multi_tenant),
                             (cls.flat_user, cls.flat_tenant)):
            for resource in ("requisition", "rfq", "quotation"):
                for action in ("view", "update"):
                    cls.grant(user, f"procurement.{resource}.{action}", tenant=tenant,
                              role_key="officer", branch=None)
        for books, branch in ((cls.multi, cls.lekki),
                              (cls.flat, cls.flat_tenant.branches.first())):
            books.requisition = PurchaseRequisition.objects.create(
                entity=books.entity, branch=branch, title="Printer paper",
                request_date=datetime.date(2026, 1, 10),
                justification="Exams", estimated_total=100000,
            )
            PurchaseRequisitionLine.objects.create(
                requisition=books.requisition, line_no=1, description="Paper",
                quantity=2, unit="ream", estimated_unit_price=50000,
            )
            books.rfq = RequestForQuotation.objects.create(
                entity=books.entity, branch=branch, title="Paper RFQ",
                issue_date=datetime.date(2026, 1, 11),
                budget_estimate=120000, notes="Buy white paper",
            )
            RfqLine.objects.create(rfq=books.rfq, line_no=1,
                                   description="Paper", quantity=2)
            books.quotation = VendorQuotation.objects.create(
                entity=books.entity, branch=branch, rfq=books.rfq,
                vendor=books.vendor, quote_date=datetime.date(2026, 1, 12),
                reference="ADE-2026", subtotal=110000, total=110000,
            )
            VendorQuotationLine.objects.create(
                quotation=books.quotation, line_no=1, description="Paper",
                quantity=2, unit_price=55000, net_amount=110000,
            )

    def test_default_and_hidden_fields_on_both_branch_shapes(self):
        for user, role, books in ((self.officer_user, self.officer_role, self.multi),
                                  (self.flat_user, self.flat_role, self.flat)):
            with self.subTest(tenant=books.entity.code):
                paths = (("requisition", books.requisition.pk, "estimated_total"),
                         ("rfq", books.rfq.pk, "budget_estimate"),
                         ("quotation", books.quotation.pk, "total"))
                endpoints = {"requisition": "requisitions", "rfq": "rfqs",
                             "quotation": "quotations"}
                for resource, pk, field in paths:
                    path = f"{endpoints[resource]}/{pk}/"
                    if resource == "rfq":
                        set_field_access(role, "procurement.rfq.budget_estimate",
                                         read=True, write=True)
                    opened = self.call(user, path, books=books)
                    self.assertEqual(opened.status_code, 200, opened.data)
                    self.assertIn(field, opened.data["data"])
                    set_field_access(role, f"procurement.{resource}.{field}",
                                     read=False, write=False)
                    hidden = self.call(user, path, books=books)
                    listed = self.call(user, f"{endpoints[resource]}/", books=books)
                    self.assertEqual(hidden.status_code, 200, hidden.data)
                    self.assertEqual(listed.status_code, 200, listed.data)
                    self.assertNotIn(field, hidden.data["data"])
                    self.assertNotIn(field, listed.data["data"][0])
                set_field_access(role, "procurement.requisition.estimated_unit_price",
                                 read=False, write=False)
                set_field_access(role, "procurement.quotation.unit_price",
                                 read=False, write=False)
                req = self.call(user, f"requisitions/{books.requisition.pk}/", books=books)
                quote = self.call(user, f"quotations/{books.quotation.pk}/", books=books)
                self.assertNotIn("estimated_unit_price", req.data["data"]["lines"][0])
                self.assertNotIn("estimated_line_total", req.data["data"]["lines"][0])
                self.assertNotIn("unit_price", quote.data["data"]["lines"][0])
                self.assertNotIn("net_amount", quote.data["data"]["lines"][0])

    def test_restricted_writes_are_denied(self):
        cases = (("requisition", "requisitions", self.multi.requisition.pk,
                  "justification", {"justification": "New need"}),
                 ("rfq", "rfqs", self.multi.rfq.pk,
                  "budget_estimate", {"budget_estimate": 200000}),
                 ("quotation", "quotations", self.multi.quotation.pk,
                  "reference", {"reference": "ADE-2027"}))
        for resource, endpoint, pk, field, body in cases:
            with self.subTest(resource=resource):
                set_field_access(self.officer_role, f"procurement.{resource}.{field}",
                                 read=True, write=False)
                response = self.call(self.officer_user, f"{endpoint}/{pk}/", "patch", body)
                self.assertEqual(response.status_code, 403, response.data)
