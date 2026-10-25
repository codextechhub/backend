"""The receipt email states what became of the money, in the receipt's own words.

Acme Ltd is billed 1,075. A payment of 400 is wholly applied to that bill. A
payment of 2,000 settles it and leaves 925 held as credit. Once the bill is
settled, a further 500 is held as credit in full. The email's settlement line
comes from the same figures as the receipt's closing sentence, and is never empty.
"""
from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from vs_finance.document_email import _receipt_context, receipt_notice_context
from vs_finance.documents import receipt_settlement_lines

from .tests_document_pdf import _DocumentFixture


class ReceiptSettlementLinesTests(SimpleTestCase):

    def test_money_all_applied_names_the_invoice(self):
        self.assertEqual(
            receipt_settlement_lines(["IV-1"], received=40000, applied=40000),
            "Applied to invoice: IV-1",
        )

    def test_several_invoices_are_listed(self):
        self.assertEqual(
            receipt_settlement_lines(["IV-1", "IV-2"], received=90000, applied=90000),
            "Applied to invoices: IV-1, IV-2",
        )

    def test_part_applied_and_part_held_says_both(self):
        self.assertEqual(
            receipt_settlement_lines(["IV-1"], received=200000, applied=107500),
            "Applied to invoice: IV-1\nHeld as credit on the account: ₦925.00",
        )

    def test_money_all_held_never_prints_an_empty_label(self):
        self.assertEqual(
            receipt_settlement_lines([], received=50000, applied=0),
            "Held as credit on the account: ₦500.00",
        )


class ReceiptEmailContextTests(_DocumentFixture, TestCase):

    def summary(self, payment):
        return _receipt_context(payment, None)["settlement_summary"]

    def test_a_receipt_fully_applied(self):
        self.assertEqual(
            self.summary(self.pay(40000)), f"Applied to invoice: {self.invoice.document_number}",
        )

    def test_a_receipt_part_held_as_credit(self):
        self.assertEqual(
            self.summary(self.pay(200000)),
            f"Applied to invoice: {self.invoice.document_number}\n"
            "Held as credit on the account: ₦925.00",
        )

    def test_a_receipt_wholly_held_as_credit(self):
        self.pay(107500)
        payment = self.pay(50000)
        self.assertEqual(self.summary(payment), "Held as credit on the account: ₦500.00")
        self.assertEqual(receipt_notice_context(payment)["settlement_summary"], self.summary(payment))
