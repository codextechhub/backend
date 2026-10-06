"""Withholding tax on a vendor payment comes from the supplier's WHT code unless typed.

Adex Construction bills Royal Crest N10,750,000 for a classroom block: N10,000,000
of work and N750,000 of VAT, and Adex carries a 5% WHT code. Left to the clerk,
one forgets WHT and the school owes the tax office N500,000 plus a penalty;
another takes 5% of the VAT-inclusive total and withholds N37,500 too much. The
system computes the N500,000 on the VAT-exclusive value, keeps a figure somebody
typed exactly as typed, and records which of the two the payment carries.
"""
from __future__ import annotations

import types
from unittest.mock import patch

from django.test import TestCase

from vs_finance.models import BankAccount
from vs_procurement.constants import WhtSource
from vs_procurement.models import VendorPayment
from vs_procurement.payables import post_vendor_invoice, resolve_wht

from .tests import _P2PFixtureMixin


class ResolveWhtTests(TestCase):
    """The one rule both supplier-payment paths use."""

    code = types.SimpleNamespace(rate_bps=500)

    def _bill(self, total, tax_total):
        return types.SimpleNamespace(total=total, tax_total=tax_total)

    def test_computed_on_the_vat_exclusive_value_of_the_bills_settled(self):
        bill = self._bill(10_750_000, 750_000)
        wht = resolve_wht(gross=10_750_000, tax_code=self.code, bills=[(bill, 10_750_000)])
        self.assertEqual((wht.amount, wht.source, wht.base), (500_000, WhtSource.COMPUTED, 10_000_000))

    def test_a_part_payment_carries_its_share_of_the_vat(self):
        bill = self._bill(10_750_000, 750_000)
        wht = resolve_wht(gross=5_375_000, tax_code=self.code, bills=[(bill, 5_375_000)])
        self.assertEqual((wht.amount, wht.base), (250_000, 5_000_000))

    def test_without_bills_the_whole_amount_is_the_base(self):
        wht = resolve_wht(gross=333_333, tax_code=self.code)
        self.assertEqual(wht.amount, 16_667)  # 16,666.65 rounds half-up to a whole kobo.

    def test_no_code_computes_zero(self):
        wht = resolve_wht(gross=1_000_000, tax_code=None)
        self.assertEqual((wht.amount, wht.source), (0, WhtSource.COMPUTED))

    def test_a_typed_figure_is_kept_even_when_zero(self):
        wht = resolve_wht(gross=1_000_000, supplied=0, tax_code=self.code)
        self.assertEqual((wht.amount, wht.source), (0, WhtSource.ENTERED))


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class VendorPaymentWhtAPITests(_P2PFixtureMixin, TestCase):
    """The manual vendor payment draft applies the supplier's WHT code."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        super().setUpTestData()
        cls.entity, _, cls.vendor, vat, wht_code = cls._p2p_books
        cls.vendor.default_wht_tax_code = wht_code
        cls.vendor.save(update_fields=["default_wht_tax_code", "updated_at"])
        bill = cls.make_bill(cls.entity, cls.vendor, [("5300", 1, 1_000_000, vat, None)])
        post_vendor_invoice(bill)
        bill.refresh_from_db()
        cls.bill = bill  # 1,000,000 of work and 75,000 of VAT.
        cls.bank = BankAccount.objects.create(
            entity=cls.entity, gl_account=cls.acc(cls.entity, "1100"), name="Operating Bank",
        )
        cls.clerk = get_user_model().objects.create_user(
            email="wht-clerk@test.com", password="pw", tenant=cls.entity.tenant,
            status="ACTIVE", first_name="Wht", last_name="Clerk",
        )

    def setUp(self):
        from core.test_utils import TenantAPIClient

        self.client = TenantAPIClient(user=self.clerk)

    def _body(self, amount, **extra):
        return {
            "vendor": self.vendor.code, "payment_date": "2026-01-15",
            "bank_account": self.bank.id, "method": "BANK_TRANSFER",
            "allocations": [{"vendor_invoice": self.bill.id, "amount": amount}],
            **extra,
        }

    def _create(self, amount, **extra):
        response = self.client.post(
            f"/v1/procurement/vendor-payments/?entity={self.entity.code}",
            self._body(amount, **extra), format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return VendorPayment.objects.get(pk=response.data["data"]["id"])

    def _edit(self, payment, amount, **extra):
        response = self.client.patch(
            f"/v1/procurement/vendor-payments/{payment.pk}/?entity={self.entity.code}",
            self._body(amount, **extra), format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payment.refresh_from_db()
        return payment

    def test_an_omitted_wht_is_computed_from_the_vendors_code(self, _permission):
        payment = self._create(1_075_000)
        self.assertEqual(payment.wht_amount, 50_000)  # 5% of 1,000,000, not of 1,075,000.
        self.assertEqual(payment.wht_source, WhtSource.COMPUTED)
        self.assertEqual(payment.net_amount, 1_025_000)
        self.assertEqual(payment.wht_tax_code.code, "WHT-5")

    def test_a_typed_wht_is_kept_and_flagged(self, _permission):
        payment = self._create(1_075_000, wht_amount=53_750)
        self.assertEqual(payment.wht_amount, 53_750)
        self.assertEqual(payment.wht_source, WhtSource.ENTERED)

    def test_an_edit_recomputes_a_computed_figure(self, _permission):
        payment = self._edit(self._create(1_075_000), 537_500)
        self.assertEqual(payment.wht_amount, 25_000)
        self.assertEqual(payment.wht_source, WhtSource.COMPUTED)

    def test_an_edit_keeps_a_typed_figure(self, _permission):
        payment = self._edit(self._create(1_075_000, wht_amount=10_000), 537_500)
        self.assertEqual(payment.wht_amount, 10_000)
        self.assertEqual(payment.wht_source, WhtSource.ENTERED)

    def test_an_edit_sending_wht_amount_null_works_a_typed_figure_out_again(self, _permission):
        payment = self._edit(self._create(1_075_000, wht_amount=10_000), 537_500, wht_amount=None)
        self.assertEqual(payment.wht_amount, 25_000)
        self.assertEqual(payment.wht_source, WhtSource.COMPUTED)
        self.assertEqual(payment.net_amount, 512_500)

    def test_an_edit_sending_wht_amount_null_without_a_code_computes_nil(self, _permission):
        payment = self._edit(
            self._create(1_075_000, wht_amount=10_000), 537_500,
            wht_amount=None, wht_tax_code=None,
        )
        self.assertEqual((payment.wht_amount, payment.wht_source), (0, WhtSource.COMPUTED))

    def test_the_payment_says_how_its_wht_was_arrived_at(self, _permission):
        url = f"/v1/procurement/vendor-payments/?entity={self.entity.code}"
        computed = self.client.post(url, self._body(1_075_000), format="json")
        typed = self.client.post(url, self._body(1_075_000, wht_amount=53_750), format="json")
        detail = self.client.get(
            f"/v1/procurement/vendor-payments/{typed.data['data']['id']}/?entity={self.entity.code}")

        self.assertEqual(computed.data["data"]["wht_source"], WhtSource.COMPUTED)
        self.assertEqual(typed.data["data"]["wht_source"], WhtSource.ENTERED)
        self.assertEqual(detail.data["data"]["wht_source"], WhtSource.ENTERED)

    def test_an_eligible_bill_carries_its_tax_so_wht_defaults_on_the_net(self, _permission):
        response = self.client.get(
            f"/v1/procurement/vendor-payments/eligible-invoices/?entity={self.entity.code}")

        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data["data"] if r["id"] == self.bill.pk)
        self.assertEqual(row["tax_total"], 75_000)
        self.assertEqual(row["subtotal"], 1_000_000)
        self.assertEqual(row["total"], 1_075_000)
