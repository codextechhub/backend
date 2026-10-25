"""A receipt closes by saying what became of the money, and says it truthfully.

Acme Ltd is billed 1,075 and has paid nothing. A payment of 400 is wholly applied
to the bill. A payment of 2,000 settles the 1,075 and leaves 925 held as credit.
Once the bill is settled, a further 500 is held as credit in full. The printed
receipt, the plain-layout PDF and the figures the template reads all take their
closing sentence from one place, so none of them says "applied" over money that
is sitting as credit.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, TestCase

from vs_finance.documents import receipt_closing_line, receipt_document_context
from vs_finance.pdf import _plain_receipt_pdf, receipt_pdf

from .tests_document_pdf import _DocumentFixture

APPLIED = "Thank you - your payment has been received and applied to your bills."
PARTLY = (
    "Thank you - your payment has been received. ₦1,075.00 was applied to your "
    "bills and ₦925.00 is held as credit on the account."
)
HELD = "Thank you - your payment has been received and is held as credit on the account."


class ReceiptClosingLineTests(SimpleTestCase):

    def test_money_all_applied(self):
        self.assertEqual(receipt_closing_line(40000, 40000), APPLIED)

    def test_money_part_applied_and_part_held(self):
        self.assertEqual(receipt_closing_line(200000, 107500), PARTLY)

    def test_money_all_held_as_credit(self):
        self.assertEqual(receipt_closing_line(50000, 0), HELD)


def _plain_layout_text(context) -> str:
    """Every paragraph and table cell the plain layout would print for ``context``."""
    captured = {}
    with mock.patch("vs_finance.pdf._build", side_effect=lambda story, **kw: captured.setdefault("story", story) or b""):
        _plain_receipt_pdf(context)

    def texts(flowable):
        if hasattr(flowable, "text"):
            yield flowable.text
        for row in getattr(flowable, "_cellvalues", None) or []:
            for cell in row:
                yield from texts(cell)

    # The plain layout spells the naira sign out, having no glyph for it.
    return " ".join(t for f in captured["story"] for t in texts(f)).replace("\u20a6", "NGN ")


@mock.patch("vs_finance.pdf._embedded_logo", return_value="")
class ReceiptClosingLineOnTheDocumentTests(_DocumentFixture, TestCase):

    def line(self, payment):
        return receipt_document_context(payment)["receipt"]["closing_line"]

    def test_a_receipt_fully_applied_says_so(self, _logo):
        payment = self.pay(40000)
        self.assertEqual(self.line(payment), APPLIED)
        self.assertEqual(receipt_document_context(payment)["receipt"]["credit_held"], "")

    def test_a_receipt_part_held_as_credit_says_so(self, _logo):
        payment = self.pay(200000)
        self.assertEqual(self.line(payment), PARTLY)
        self.assertEqual(receipt_document_context(payment)["receipt"]["credit_held"], "₦925.00")

    def test_a_receipt_wholly_held_as_credit_says_so(self, _logo):
        self.pay(107500)
        payment = self.pay(50000)
        self.assertEqual(payment.allocations.count(), 0)
        self.assertEqual(self.line(payment), HELD)
        self.assertEqual(receipt_document_context(payment)["receipt"]["credit_held"], "₦500.00")

    def test_the_printed_receipt_carries_the_sentence_and_the_credit_row(self, _logo):
        html = self.rendered_html(receipt_pdf, self.pay(200000))
        self.assertIn(PARTLY, html)
        self.assertNotIn("received and applied.", html)
        self.assertIn("Held as credit on the account", html)

    def test_the_printed_receipt_of_money_wholly_held_names_no_invoice(self, _logo):
        self.pay(107500)
        html = self.rendered_html(receipt_pdf, self.pay(50000))
        self.assertIn(HELD, html)
        self.assertNotIn("Unallocated credit", html)
        self.assertIn("Held as credit on the account", html)

    def test_the_plain_layout_pdf_says_the_same(self, _logo):
        text = _plain_layout_text(receipt_document_context(self.pay(200000)))
        self.assertIn(PARTLY.replace("\u20a6", "NGN "), text)
        self.assertIn("Held as credit on the account", text)

    def test_the_plain_layout_pdf_of_money_wholly_held(self, _logo):
        self.pay(107500)
        text = _plain_layout_text(receipt_document_context(self.pay(50000)))
        self.assertIn(HELD, text)

    def test_the_plain_layout_pdf_of_money_fully_applied(self, _logo):
        text = _plain_layout_text(receipt_document_context(self.pay(40000)))
        self.assertIn(APPLIED, text)
