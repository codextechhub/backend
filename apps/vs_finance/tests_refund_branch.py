"""A refund pays out only the credit of its own branch.

The Okafor family is one school-wide customer. Lekki banked 20,000 kobo for them
on 12 January and Ikeja 30,000 on 15 January, and neither receipt has been
applied, so the family holds 50,000 of credit: 30,000 at Ikeja and 20,000 at
Lekki.

A refund's journal is booked to its own branch. Ikeja paying back money Lekki
received would overdraw Ikeja's customer credit and leave Lekki's standing, so:

* an Ikeja refund draws Ikeja's receipt, never Lekki's older one;
* asking Ikeja to refund more than Ikeja holds is refused with a 400 naming the
  branch that holds the rest, whoever asks;
* the refund screens offer each branch's credit as its own row and total, within
  the branches the reader can raise a refund for, so a bursar is never shown money
  she cannot pay out;
* a branch-bound bursar neither sees nor pays out unbranched (school-wide) money;
  a whole-school user does.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.exceptions import SettlementBranchError
from vs_finance.models import Account, Payment, Refund

from .tests_branch_scope import _FinanceBranchFixture

KEYS = ("finance.refund.view", "finance.refund.create", "finance.customer.view")


class _RefundFixture(_FinanceBranchFixture):
    def setUp(self):
        from vs_finance.receivables import post_payment

        super().setUp()
        e = self.books
        self.bank = Account.objects.get(entity=e, code="1100")
        self.family = self.customer(e, "OKAFOR", None)

        def receipt(branch, amount, day):
            payment = Payment.objects.create(
                entity=e, customer=self.family, branch=branch,
                payment_date=datetime.date(2026, 1, day), amount=amount,
                deposit_account=self.bank,
            )
            post_payment(payment, auto_allocate=False)
            payment.refresh_from_db()
            return payment

        self.lekki_receipt = receipt(self.lekki, 20_000, 12)
        self.ikeja_receipt = receipt(self.ikeja, 30_000, 15)

        self.head = self.client_for("head.bursar@example.com", "refund-head")
        self.ikeja_bursar = self.client_for(
            "ikeja.bursar@example.com", "refund-ikeja", branch=self.ikeja)

    def client_for(self, email, role_key, *, branch=None):
        user = self.grant(
            self.user_for(self.tenant, email), *KEYS,
            tenant=self.tenant, role_key=role_key, branch=branch,
        )
        return TenantAPIClient(user=user)

    def school_wide_receipt(self, amount):
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity=self.books, customer=self.family, branch=None,
            payment_date=datetime.date(2026, 1, 16), amount=amount,
            deposit_account=self.bank,
        )
        post_payment(payment, auto_allocate=False)
        return payment

    def refund(self, branch, amount):
        return Refund.objects.create(
            entity=self.books, customer=self.family, branch=branch,
            refund_date=datetime.date(2026, 1, 20), amount=amount,
            deposit_account=self.bank,
        )

    def create(self, client, amount, branch=None):
        body = {"customer": self.family.code, "amount": amount, "refund_date": "2026-01-20"}
        if branch is not None:
            body["branch"] = branch.pk
        return client.post(
            f"/v1/finance/refunds/?entity={self.books.code}", body, format="json")


class RefundPayoutTests(_RefundFixture):
    def test_a_branch_refund_draws_its_own_branchs_credit_only(self):
        from vs_finance.credit_notes import post_refund

        post_refund(self.refund(self.ikeja, 30_000))

        self.ikeja_receipt.refresh_from_db()
        self.lekki_receipt.refresh_from_db()
        self.assertEqual(self.ikeja_receipt.refunded_amount, 30_000)
        self.assertEqual(self.lekki_receipt.refunded_amount, 0)

    def test_a_refund_beyond_its_branchs_credit_names_the_branch_that_holds_it(self):
        from vs_finance.credit_notes import post_refund

        with self.assertRaisesMessage(
            SettlementBranchError,
            "This refund belongs to Ikeja Branch and OKAFOR's refundable credit of "
            "₦200.00 is held by Lekki Branch. Raise the refund for Lekki Branch.",
        ):
            post_refund(self.refund(self.ikeja, 40_000))

        self.lekki_receipt.refresh_from_db()
        self.assertEqual(self.lekki_receipt.refunded_amount, 0)


class RefundCreationTests(_RefundFixture):
    def test_a_whole_school_bursar_is_refused_a_cross_branch_refund_with_a_400(self):
        response = self.create(self.head, 40_000, branch=self.ikeja)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "SETTLEMENT_BRANCH")
        self.assertIn("held by Lekki Branch", response.data["message"])
        self.assertFalse(Refund.objects.filter(customer=self.family).exists())

    def test_naming_the_branch_that_holds_the_credit_raises_the_refund_there(self):
        response = self.create(self.head, 20_000, branch=self.lekki)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Refund.objects.get(customer=self.family).branch_id, self.lekki.pk)

    def test_a_school_wide_refund_cannot_pay_out_branch_credit(self):
        response = self.create(self.head, 10_000)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "SETTLEMENT_BRANCH")
        self.assertIn("This refund is school-wide", response.data["message"])

    def test_a_branch_bursar_never_raises_a_school_wide_refund(self):
        """Naming no branch for a school-wide family gives her own branch's refund."""
        self.school_wide_receipt(15_000)

        response = self.create(self.ikeja_bursar, 10_000)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Refund.objects.get(customer=self.family).branch_id, self.ikeja.pk)

    def test_a_branch_bursar_cannot_see_or_post_a_school_wide_refund(self):
        refund = self.refund(None, 10_000)

        response = self.ikeja_bursar.get(
            f"/v1/finance/refunds/{refund.pk}/?entity={self.books.code}")

        self.assertEqual(response.status_code, 404, response.data)

    def test_a_branch_bursar_cannot_raise_a_refund_for_another_branch(self):
        response = self.create(self.ikeja_bursar, 20_000, branch=self.lekki)

        self.assertEqual(response.status_code, 403, response.data)


class RefundScreenTests(_RefundFixture):
    def availability(self, client):
        response = client.get(f"/v1/finance/refunds/availability/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {
            (row["customer_code"], row["branch_id"]): row["refundable_credit"]
            for row in response.data["data"]
        }

    def test_each_branchs_credit_is_its_own_row(self):
        self.assertEqual(self.availability(self.head), {
            ("OKAFOR", self.ikeja.pk): 30_000,
            ("OKAFOR", self.lekki.pk): 20_000,
        })

    def test_a_branch_bursar_is_offered_only_her_branchs_credit(self):
        self.assertEqual(self.availability(self.ikeja_bursar), {
            ("OKAFOR", self.ikeja.pk): 30_000,
        })

    def test_school_wide_credit_is_offered_to_a_whole_school_user_only(self):
        self.school_wide_receipt(15_000)

        self.assertEqual(self.availability(self.head), {
            ("OKAFOR", self.ikeja.pk): 30_000,
            ("OKAFOR", self.lekki.pk): 20_000,
            ("OKAFOR", None): 15_000,
        })
        self.assertEqual(self.availability(self.ikeja_bursar), {
            ("OKAFOR", self.ikeja.pk): 30_000,
        })

    def test_the_refundable_credit_card_is_what_the_reader_can_refund(self):
        self.school_wide_receipt(15_000)
        response = self.ikeja_bursar.get(
            f"/v1/finance/ar-adjustments/?entity={self.books.code}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["kpis"]["refundable_credit"], 30_000)
