"""A payout with WHT withheld is read at what it sent, where money is set against money.

Corona pays Ojo Stationers a N100 bill line and withholds N5 of WHT, so N95 leaves
the bank and the bank statement shows N95. Settlement reconciliation must pair the
payout with that N95 line, and must not read the N5 withheld as a provider fee. The
movements feed shows the N95 that moved, with the N100 line and the N5 WHT beside
it, and its header counts N95 out.
"""
from __future__ import annotations

import datetime

from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, BankAccount, BankStatementLine
from vs_finance.tests_branch_scope import _FinanceBranchFixture

from .constants import PayoutStatus
from .models import PayoutInstruction
from .services import payout_sent_amount, payout_sent_expression

LINE, WHT, SENT = 10_000, 500, 9_500


class PayoutNetTests(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        cash_type = Account.objects.get(entity=self.books, code="1000").account_type
        gl = Account.objects.create(entity=self.books, code="1180", name="Payouts",
                                    account_type=cash_type, is_postable=True)
        self.bank = BankAccount.objects.create(entity=self.books, name="Payouts", gl_account=gl)
        user = self.grant(self.user_for(self.tenant, "net-reader@corona.test"),
                          "payments.report.view", tenant=self.tenant, role_key="net-reader")
        self.client = TenantAPIClient(user=user)

    def payout(self, reference, *, status=PayoutStatus.PAID, **metadata):
        return PayoutInstruction.objects.create(
            entity=self.books, provider="PAYSTACK", reference=reference, amount=LINE,
            beneficiary_name="Ojo Stationers", beneficiary_account_number="0123456789",
            status=status, confirmed_at=timezone.now() if status == PayoutStatus.PAID else None,
            metadata={"wht_amount": WHT, **metadata})

    def bank_line(self, amount, reference):
        return BankStatementLine.objects.create(
            bank_account=self.bank, txn_date=datetime.date.today(), amount=amount,
            description="Transfer", reference=reference)

    def get(self, path):
        response = self.client.get(f"/v1/payments/{path}?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    # -- settlement reconciliation -------------------------------------------- #

    def reconciliation(self):
        data = self.get("reports/settlement-reconciliation/")["data"]
        return {row["reference"]: row for row in data["rows"]}, data

    def test_a_payout_pairs_with_the_net_the_bank_shows(self):
        self.payout("PAY-WHT", transfer_amount=SENT)
        self.bank_line(-SENT, "NIP/000123")

        rows, data = self.reconciliation()
        row = rows["PAY-WHT"]
        self.assertEqual(row["amount"], -SENT)
        self.assertTrue(row["settled"])
        self.assertEqual(row["match_basis"], "amount")
        self.assertEqual(row["fee_amount"], 0)
        self.assertEqual(data["unmatched_bank_lines"], [])

    def test_the_wht_withheld_is_not_read_as_a_provider_fee(self):
        self.payout("PAY-WHT", transfer_amount=SENT)
        self.bank_line(-SENT, "PAY-WHT")

        row = self.reconciliation()[0]["PAY-WHT"]
        self.assertEqual((row["match_basis"], row["settled_amount"], row["fee_amount"]),
                         ("reference", -SENT, 0))

    # -- movements ------------------------------------------------------------ #

    def test_the_feed_shows_what_moved_with_the_line_and_the_wht_beside_it(self):
        self.payout("PAY-WHT", transfer_amount=SENT)
        self.payout("PAY-QUEUED", status=PayoutStatus.PENDING)

        rows = {row["reference"]: row for row in self.get("movements/")["data"]}
        for reference in ("PAY-WHT", "PAY-QUEUED"):
            with self.subTest(reference=reference):
                row = rows[reference]
                self.assertEqual((row["amount"], row["gross_amount"], row["wht_amount"]),
                                 (SENT, LINE, WHT))
                self.assertEqual(row["amount_naira"], "₦95.00")

        self.assertEqual(self.get("movements/summary/")["data"]["out7d"]["kobo"], SENT)

    def test_the_query_and_the_record_agree_on_what_was_sent(self):
        cases = {
            "PAY-DISPATCHED": (self.payout("PAY-DISPATCHED", transfer_amount=SENT), SENT),
            "PAY-SHORT": (self.payout("PAY-SHORT", transfer_amount=SENT,
                                      provider_sent_amount=9_000), 9_000),
            "PAY-QUEUED": (self.payout("PAY-QUEUED", status=PayoutStatus.PENDING), SENT),
            # Dispatched before the net was recorded: the gross is what was sent.
            "PAY-LEGACY": (self.payout("PAY-LEGACY"), LINE),
        }
        annotated = dict(PayoutInstruction.objects.filter(entity=self.books).annotate(
            sent=payout_sent_expression()).values_list("reference", "sent"))
        for reference, (payout, expected) in cases.items():
            with self.subTest(reference=reference):
                self.assertEqual(payout_sent_amount(payout), expected)
                self.assertEqual(annotated[reference], expected)
