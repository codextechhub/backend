"""Receipt and petty cash endpoints apply their field decisions beyond the list row.

Ikeja and Lekki have separate cash tins, while a single-branch school uses the
same routes without a branch selector. A hidden figure must leave the detail,
summary and status payloads, and a denied Write must stop before a row is saved.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .models import Account, Payment, PettyCashVoucher, PettyCashVoucherLine
from .tests_petty_cash_returns import _PettyCashReturnFixture


class ReceiptAndPettyCashFieldAccessTests(_PettyCashReturnFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.customer_row = cls.customer(cls.books, "REC-CLIENT", cls.ikeja)
        cls.payment = Payment.objects.create(
            entity=cls.books, branch=cls.ikeja, customer=cls.customer_row,
            payment_date=datetime.date(2026, 1, 10), amount=84_000,
            deposit_account=Account.objects.get(entity=cls.books, code="1100"),
            status="POSTED", reference="REC-84", narration="Term fees",
        )
        cls.voucher = PettyCashVoucher.objects.create(
            entity=cls.books, branch=cls.ikeja, fund=cls.fund,
            voucher_date=datetime.date(2026, 1, 10), payee="Mama Tunde",
            narration="Chalk", reference="V-4500",
        )
        PettyCashVoucherLine.objects.create(
            voucher=cls.voucher, line_no=1, description="Chalk",
            expense_account=Account.objects.get(entity=cls.books, code="5300"),
            quantity=1, unit_price=4_500,
        )
        for resource in ("finance.payment", "finance.pettycash", "finance.pettycashvoucher"):
            install_declared_fields(resource)
        cls.reader = cls.grant(
            cls.user_for(cls.tenant, "area-e-reader@corona.test"),
            "finance.payment.view", "finance.payment.create",
            "finance.pettycash.view", "finance.pettycash.create",
            "finance.pettycash.update", "finance.pettycash.reopen",
            "finance.pettycashvoucher.view", "finance.pettycashvoucher.create",
            tenant=cls.tenant, role_key="area-e-reader", branch=cls.ikeja,
        )
        cls.role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="area-e-reader")

    def url(self, suffix):
        return f"/v1/finance/{suffix}?entity={self.books.code}"

    def api(self):
        return TenantAPIClient(user=self.reader)

    def hide(self, *keys):
        set_field_access(self.role, *keys, read=False, write=False)

    def test_receipt_amount_closes_on_list_detail_and_summary(self):
        self.hide("finance.payment.amount")
        listed = self.api().get(self.url("payments/"))
        self.assertEqual(listed.status_code, 200, listed.data)
        row = listed.data["data"][0]
        self.assertNotIn("amount", row)
        self.assertNotIn("amount_naira", row)
        detail = self.api().get(self.url(f"payments/{self.payment.pk}/"))
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertNotIn("amount", detail.data["data"]["payment"])
        self.assertNotIn("gl_postings", detail.data["data"])
        summary = self.api().get(self.url("payments/summary/"))
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertNotIn("today", summary.data["data"])
        self.assertNotIn("week", summary.data["data"])

    def test_receipt_create_refuses_a_closed_amount(self):
        self.hide("finance.payment.amount")
        response = self.api().post(
            self.url(f"customers/{self.customer_row.pk}/receipt/"),
            {"amount": 3_000, "payment_date": "2026-01-10"}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Payment.objects.filter(entity=self.books).count(), 1)

    def test_refund_switch_closes_summary_classification(self):
        self.hide("finance.payment.refunded_amount")
        summary = self.api().get(self.url("payments/summary/"))
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertNotIn("refunded", summary.data["data"])
        self.assertNotIn("status_counts", summary.data["data"])

    def test_fund_figures_close_on_detail_and_status(self):
        self.hide("finance.pettycash.float_amount", "finance.pettycash.cash_on_hand")
        detail = self.api().get(self.url(f"petty-cash-funds/{self.fund.pk}/"))
        self.assertEqual(detail.status_code, 200, detail.data)
        for key in ("float_amount", "float_amount_naira", "current_balance",
                    "current_balance_naira", "register"):
            self.assertNotIn(key, detail.data["data"])
        status = self.api().get(self.url("petty-cash-status/"))
        self.assertEqual(status.status_code, 200, status.data)
        (row,) = status.data["data"]["rows"]
        for key in ("float_amount", "current_balance", "shortfall", "needs_replenish"):
            self.assertNotIn(key, row)

    def test_fund_change_refuses_a_closed_float(self):
        self.hide("finance.pettycash.float_amount")
        response = self.api().patch(
            self.url(f"petty-cash-funds/{self.fund.pk}/"),
            {"float_amount": 75_000}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.fund.refresh_from_db()
        self.assertNotEqual(self.fund.float_amount, 75_000)

    def test_voucher_payee_closes_on_reads_and_create(self):
        self.hide("finance.pettycashvoucher.payee")
        listed = self.api().get(self.url("petty-cash-vouchers/"))
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertNotIn("payee", listed.data["data"][0])
        detail = self.api().get(self.url(f"petty-cash-vouchers/{self.voucher.pk}/"))
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertNotIn("payee", detail.data["data"])
        response = self.api().post(
            self.url("petty-cash-vouchers/"),
            {"payee": "Another payee"}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(PettyCashVoucher.objects.filter(entity=self.books).count(), 1)
