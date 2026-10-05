"""A goods return's journal points to its receipt, where the correction is made.

Ikeja receives 10 boxes of exercise books and sends 2 back as a goods return. A
return is not voided: it is itself how a receipt is undone, and what went back did
go back. If the 2 boxes never left, or came back from the vendor, they are received
again against the same order. So the journal of the return names the return and
the receipt it came off, and the receipt's screen lists its returns.
"""
from __future__ import annotations

from decimal import Decimal

from core.test_utils import TenantAPIClient
from vs_finance.exceptions import PostingError
from vs_finance.posting import journal_reversal_action, reverse_journal

from .corrections import return_goods
from .tests_ap_corrections import JAN, _APCorrectionsFixture


class GoodsReturnJournalActionTests(_APCorrectionsFixture):

    def setUp(self):
        po = self.order(10, 50_000)
        self.grn = self.receive(po, 10)
        self.goods_return = return_goods(
            self.grn, lines=[(self.grn.lines.get().pk, Decimal("2"))],
            return_date=JAN(2026, 1, 20), reason="Two boxes damaged")

    def test_the_journal_names_the_return_its_receipt_and_the_correction(self):
        action = journal_reversal_action(self.goods_return.journal)

        self.assertEqual(action["kind"], "SOURCE_DOCUMENT_ACTION")
        self.assertEqual(action["document_type"], "GoodsReturn")
        self.assertEqual(action["document_id"], self.goods_return.pk)
        self.assertEqual(action["document_number"], self.goods_return.document_number)
        self.assertEqual(action["receipt"], {
            "document_type": "GOODS_RECEIVED_NOTE", "document_id": self.grn.pk,
            "document_number": self.grn.document_number,
        })
        self.assertEqual(action["correction"], "RECEIVE_AGAIN")

    def test_reversing_the_journal_by_hand_says_to_receive_again(self):
        with self.assertRaises(PostingError) as caught:
            reverse_journal(self.goods_return.journal)

        self.assertIn("receive them again", str(caught.exception))
        self.assertIn(self.grn.document_number, str(caught.exception))

    def test_the_receipt_lists_its_returns(self):
        reader = self.grant(
            self.user_for(self.tenant, "storekeeper@corona.test"),
            "procurement.goods_receipt.view", tenant=self.tenant, role_key="gr-reader",
        )

        response = TenantAPIClient(user=reader).get(
            f"/v1/procurement/goods-receipts/{self.grn.pk}/?entity={self.books.code}")

        self.assertEqual(response.status_code, 200, response.data)
        returns = response.data["data"]["returns"]
        self.assertEqual([r["id"] for r in returns], [self.goods_return.pk])
        self.assertEqual(returns[0]["reason"], "Two boxes damaged")
        self.assertEqual(Decimal(returns[0]["lines"][0]["quantity"]), Decimal("2"))

    def test_another_branch_cannot_read_the_receipt_or_its_returns(self):
        reader = self.grant(
            self.user_for(self.tenant, "lekki.store@corona.test"),
            "procurement.goods_receipt.view", tenant=self.tenant, role_key="gr-lekki",
            branch=self.lekki,
        )

        response = TenantAPIClient(user=reader).get(
            f"/v1/procurement/goods-receipts/{self.grn.pk}/?entity={self.books.code}")

        self.assertEqual(response.status_code, 404)
