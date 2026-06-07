"""HTTP tests for the /v1/procurement/ REST surface.

Drives the whole Procure-to-Pay chain through the real URLs as a Vision super admin
(who bypasses RBAC), proving the endpoints wire the purchasing/payables services
correctly: a requisition becomes a PO, goods are received (Dr expense, Cr GR/IR), the
bill clears GR/IR and raises AP, and the payment settles AP — after which the AP
sub-ledger reconciles to the control account and GR/IR nets to zero.

Kept in a separate module from the Phase-3 service tests (``tests.py``) so the service
suite stays a pure-Python contract test while this one exercises the API envelope.
"""
from __future__ import annotations

import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from vs_finance.models import Account, FiscalPeriod, FiscalYear, LedgerEntity, TaxCode
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.models import PurchaseOrder, Vendor


class _ProcAPIFixture:
    """Seeds an entity (chart + open year), a vendor and tax codes, and a super admin."""

    def build(self):
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        today = datetime.date.today()
        year = FiscalYear.objects.create(
            entity=entity, year=today.year,
            start_date=datetime.date(today.year, 1, 1),
            end_date=datetime.date(today.year, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name=f"FY{today.year}",
            start_date=datetime.date(today.year, 1, 1),
            end_date=datetime.date(today.year, 12, 31),
        )
        vendor = Vendor.objects.create(
            entity=entity, code="SUPP1", name="Supplier One",
            payable_account=self.acc(entity, "2100"),
            default_expense_account=self.acc(entity, "5300"),
        )
        TaxCode.objects.create(
            entity=entity, code="VAT-IN", name="Input VAT 7.5%", rate_bps=750,
            paid_account=self.acc(entity, "1300"),
        )
        return entity, vendor, today

    @staticmethod
    def acc(entity, code):
        return Account.objects.get(entity=entity, code=code)

    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        User = get_user_model()
        self.user = User.objects.create_user(
            email="proc-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Proc", last_name="Admin",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.user, role=role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)


class VendorAPITests(_ProcAPIFixture, TestCase):
    def test_create_and_list_vendor(self):
        entity, _, _ = self.build()
        resp = self.client.post(
            f"/v1/procurement/vendors/?entity={entity.code}",
            {"code": "ACME", "name": "Acme Ltd", "payable_account": "2100",
             "default_expense_account": "5300", "payment_terms": "NET_30"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["code"], "ACME")

        listing = self.client.get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertEqual(listing.status_code, 200)
        codes = {v["code"] for v in listing.json()["data"]}
        self.assertIn("ACME", codes)
        self.assertIn("SUPP1", codes)


class P2PChainAPITests(_ProcAPIFixture, TestCase):
    def _q(self, path):
        return f"/v1/procurement/{path}?entity={self.entity.code}"

    def test_full_chain_requisition_to_payment(self):
        self.entity, vendor, today = self.build()
        d = today.isoformat()

        # 1. Requisition (one line, ₦1,000,000 = 10 @ ₦100,000) → submit → approve.
        r = self.client.post(self._q("requisitions/"), {
            "request_date": d, "justification": "Lab supplies",
            "lines": [{"description": "Beakers", "quantity": 10,
                       "estimated_unit_price": 100000, "expense_account": "5100"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        req_id = r.json()["data"]["id"]
        self.assertEqual(r.json()["data"]["estimated_total"], 1_000_000)

        self.assertEqual(self.client.post(self._q(f"requisitions/{req_id}/submit/")).status_code, 200)
        self.assertEqual(self.client.post(self._q(f"requisitions/{req_id}/approve/")).status_code, 200)

        # 2. Purchase order from the approved requisition.
        po_resp = self.client.post(self._q("purchase-orders/"), {
            "requisition": req_id, "vendor": "SUPP1", "order_date": d,
        }, format="json")
        self.assertEqual(po_resp.status_code, 201, po_resp.content)
        po_id = po_resp.json()["data"]["id"]
        po_line_id = PurchaseOrder.objects.get(pk=po_id).lines.first().id

        # 3. Goods receipt (all 10 accepted) → post: Dr 5100, Cr 2150 (GR/IR).
        grn_resp = self.client.post(self._q("goods-receipts/"), {
            "vendor": "SUPP1", "purchase_order": po_id, "received_date": d,
            "lines": [{"po_line": po_line_id, "accepted_qty": 10}],
        }, format="json")
        self.assertEqual(grn_resp.status_code, 201, grn_resp.content)
        grn_id = grn_resp.json()["data"]["id"]
        posted = self.client.post(self._q(f"goods-receipts/{grn_id}/post/"))
        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(posted.json()["data"]["status"], "POSTED")

        # GR/IR now holds the uninvoiced liability.
        grir = self.client.get(self._q("reports/grir/")).json()["data"]
        self.assertEqual(grir["grir_balance"]["kobo"], 1_000_000)

        # 4. Vendor invoice (matched to the PO line) → match → post: clears GR/IR, raises AP.
        bill_resp = self.client.post(self._q("vendor-invoices/"), {
            "vendor": "SUPP1", "purchase_order": po_id, "invoice_date": d, "due_date": d,
            "vendor_reference": "INV-7788",
            "lines": [{"po_line": po_line_id, "expense_account": "5100",
                       "quantity": 10, "unit_price": 100000}],
        }, format="json")
        self.assertEqual(bill_resp.status_code, 201, bill_resp.content)
        bill_id = bill_resp.json()["data"]["id"]

        match = self.client.post(self._q(f"vendor-invoices/{bill_id}/match/"))
        self.assertEqual(match.json()["data"]["match_status"], "AUTO_MATCHED")
        post_bill = self.client.post(self._q(f"vendor-invoices/{bill_id}/post/"))
        self.assertEqual(post_bill.status_code, 200, post_bill.content)

        # Goods received AND invoiced → GR/IR nets to zero.
        grir = self.client.get(self._q("reports/grir/")).json()["data"]
        self.assertTrue(grir["is_clear"])

        # 5. Vendor payment (gross ₦1,000,000, no WHT) → post + auto-allocate to the bill.
        pay_resp = self.client.post(self._q("vendor-payments/"), {
            "vendor": "SUPP1", "payment_date": d, "gross_amount": 1_000_000,
            "payment_account": "1100",
        }, format="json")
        self.assertEqual(pay_resp.status_code, 201, pay_resp.content)
        pay_id = pay_resp.json()["data"]["id"]
        post_pay = self.client.post(self._q(f"vendor-payments/{pay_id}/post/"),
                                    {"auto_allocate": True}, format="json")
        self.assertEqual(post_pay.status_code, 200, post_pay.content)
        self.assertEqual(post_pay.json()["data"]["status"], "POSTED")

        # 6. The bill is fully paid; AP sub-ledger reconciles to the control account.
        bill = self.client.get(self._q(f"vendor-invoices/{bill_id}/")).json()["data"]
        self.assertEqual(bill["payment_status"], "PAID")
        self.assertEqual(bill["balance_due"], 0)

        recon = self.client.get(self._q("reports/ap-reconciliation/")).json()["data"]
        self.assertTrue(recon["is_reconciled"])
        self.assertEqual(recon["difference"]["kobo"], 0)

    def test_over_billed_post_is_blocked(self):
        self.entity, vendor, today = self.build()
        d = today.isoformat()
        # PR → PO for 5 units, receive 5, but bill for 6 → OVER_BILLED, post blocked.
        req_id = self.client.post(self._q("requisitions/"), {
            "request_date": d,
            "lines": [{"description": "Chairs", "quantity": 5,
                       "estimated_unit_price": 50000, "expense_account": "5100"}],
        }, format="json").json()["data"]["id"]
        self.client.post(self._q(f"requisitions/{req_id}/submit/"))
        self.client.post(self._q(f"requisitions/{req_id}/approve/"))
        po_id = self.client.post(self._q("purchase-orders/"), {
            "requisition": req_id, "vendor": "SUPP1", "order_date": d,
        }, format="json").json()["data"]["id"]
        po_line_id = PurchaseOrder.objects.get(pk=po_id).lines.first().id

        grn_id = self.client.post(self._q("goods-receipts/"), {
            "vendor": "SUPP1", "purchase_order": po_id, "received_date": d,
            "lines": [{"po_line": po_line_id, "accepted_qty": 5}],
        }, format="json").json()["data"]["id"]
        self.client.post(self._q(f"goods-receipts/{grn_id}/post/"))

        bill_id = self.client.post(self._q("vendor-invoices/"), {
            "vendor": "SUPP1", "purchase_order": po_id, "invoice_date": d,
            "lines": [{"po_line": po_line_id, "expense_account": "5100",
                       "quantity": 6, "unit_price": 50000}],
        }, format="json").json()["data"]["id"]
        self.client.post(self._q(f"vendor-invoices/{bill_id}/match/"))
        blocked = self.client.post(self._q(f"vendor-invoices/{bill_id}/post/"))
        self.assertEqual(blocked.status_code, 409, blocked.content)


class ProcurementAuthTests(_ProcAPIFixture, TestCase):
    def test_requires_entity_param(self):
        self.build()
        resp = self.client.get("/v1/procurement/vendors/")
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_is_rejected(self):
        entity, _, _ = self.build()
        anon = APIClient()
        resp = anon.get(f"/v1/procurement/vendors/?entity={entity.code}")
        self.assertIn(resp.status_code, (401, 403))
