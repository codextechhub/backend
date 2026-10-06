"""An approved expense claim is reimbursed in parts, and a figure that cannot be paid is refused.

Mrs Okafor's ₦5,000 taxi claim is approved. Mr Bassey, Ikeja's cashier, pays her
₦2,000 on the 20th: the claim reads Part-paid with ₦3,000 left. He pays the
₦3,000 on the 25th and it reads Paid. Had he typed ₦6,000, he is told ₦5,000
is left and nothing is paid; ₦0 is refused the same way. A claim still a
draft, one sent back to Mrs Okafor and one with its approvers cannot be
reimbursed at all, and the refusal says which. ``amount`` is integer kobo, as
every finance money field is.
"""
from __future__ import annotations

from .constants import DocumentStatus
from .models import ExpenseClaim
from .tests_list_words import _WordsFixture

SETTLE_KEYS = ("finance.expenseclaim.settle", "finance.expenseclaim.view")


class ClaimPartPaymentTests(_WordsFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cashier = cls.bursar(cls.ikeja, keys=SETTLE_KEYS)
        cls.onlooker = cls.bursar(cls.ikeja, keys=("finance.expenseclaim.view",))

    def claim(self, *, submit=False, status=None, end=None):
        made = self.client_.post(self.url("expense-claims/"), {
            "claimant_name": "Mrs Okafor", "claim_date": "2026-01-10", "title": "Bank visit",
            "lines": [{"description": "Taxi", "expense_account": "5300",
                       "quantity": 1, "unit_price": 5_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        if submit:
            sent = self.client_.post(self.url(f"expense-claims/{pk}/submit/"), {}, format="json")
            self.assertEqual(sent.status_code, 200, sent.data)
        if end:
            self.decide(ExpenseClaim, pk, end)
        if status:
            ExpenseClaim.objects.filter(pk=pk).update(status=status)
        return pk

    def settle(self, pk, amount=None, *, user=None, day="2026-01-20"):
        body = {"pay_date": day, "bank_account": self.ikeja_bank.pk}
        if amount is not None:
            body["amount"] = amount
        return self.as_(user or self.cashier).post(
            self.url(f"expense-claims/{pk}/settle/"), body, format="json")

    def test_a_claim_is_part_paid_and_a_second_payment_completes_it(self):
        pk = self.claim(status=DocumentStatus.POSTED)

        first = self.settle(pk, 2_000_00)

        self.assertEqual(first.status_code, 200, first.data)
        row = first.data["data"]
        self.assertEqual((row["payment_status"], row["amount_paid"], row["balance_due"]),
                         ("PARTIAL", 2_000_00, 3_000_00))
        self.assertEqual(self.listed("expense-claims/", display_status="PART_PAID") & {pk}, {pk})

        second = self.settle(pk, 3_000_00, day="2026-01-25")

        self.assertEqual(second.status_code, 200, second.data)
        row = second.data["data"]
        self.assertEqual((row["payment_status"], row["amount_paid"], row["balance_due"]),
                         ("PAID", 5_000_00, 0))
        again = self.settle(pk, 1_00)
        self.assertEqual(again.status_code, 422, again.data)
        self.assertIn("already reimbursed in full", again.data["message"])

    def test_more_than_is_due_or_nothing_is_refused_and_nothing_is_paid(self):
        pk = self.claim(status=DocumentStatus.POSTED)

        over = self.settle(pk, 6_000_00)
        nothing = self.settle(pk, 0)

        self.assertEqual(over.status_code, 422, over.data)
        self.assertIn("has ₦5,000.00 left to reimburse, so ₦6,000.00 is more than is due",
                      over.data["message"])
        self.assertEqual(nothing.status_code, 422, nothing.data)
        self.assertIn("greater than ₦0.00", nothing.data["message"])
        claim = ExpenseClaim.objects.get(pk=pk)
        self.assertEqual((claim.amount_paid, claim.journal_id is None), (0, True))

    def test_a_claim_not_approved_is_refused_and_says_where_it_stands(self):
        cases = {
            "is still a draft": self.claim(),
            "is waiting for approval": self.claim(submit=True),
            "was sent back to whoever raised it": self.claim(submit=True, end="RETURNED"),
        }
        for words, pk in cases.items():
            with self.subTest(words=words):
                refused = self.settle(pk, 1_000_00)
                self.assertEqual(refused.status_code, 422, refused.data)
                self.assertIn(words, refused.data["message"])
                self.assertIn("Only an approved claim can be reimbursed.", refused.data["message"])
                self.assertEqual(ExpenseClaim.objects.get(pk=pk).amount_paid, 0)

    def test_without_the_settle_key_nothing_is_paid(self):
        pk = self.claim(status=DocumentStatus.POSTED)

        refused = self.settle(pk, 1_000_00, user=self.onlooker)

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(ExpenseClaim.objects.get(pk=pk).amount_paid, 0)
