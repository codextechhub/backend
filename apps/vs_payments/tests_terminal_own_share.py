"""Money taken on a branch terminal at a held tenant is the branch's, never the platform's own share.

Mrs Okeye pays N150,000 at Corona Lekki's terminal and Paystack keeps N2,000. The
platform holds N148,000 for Lekki exactly as for a checkout, so of a Paystack
balance of N178,000 only N30,000 is CodeX's own. A supplier Lekki pays out of that
terminal money carries its transfer fee, reserved with it, and the held-ledger
check still balances.
"""
from __future__ import annotations

from unittest.mock import patch

from . import held, held_reconciliation
from .constants import PayoutStatus
from .tests_terminal_payments import _PaymentFixture

FEE = 1_000


class TerminalMoneyIsHeldTests(_PaymentFixture):

    def setUp(self):
        super().setUp()
        self.fake.transfer_fee_kobo = FEE
        notify = patch.object(held_reconciliation, "_notify_operators", return_value=1)
        notify.start()
        self.addCleanup(notify.stop)

    def test_terminal_money_at_a_held_tenant_is_not_the_platforms_own_share(self):
        self.pay(self.lekki_terminal, 150_000, fee=2_000)
        self.assertEqual(held.held_balance(self.lekki.pk), 148_000)
        self.fake.balances["NGN"] = 148_000 + 30_000
        self.assertEqual(held.own_share_available(self.fake), 30_000)

    def test_a_supplier_paid_from_terminal_money_carries_its_fee_and_the_check_balances(self):
        self.pay(self.lekki_terminal, 150_000, fee=2_000)
        payout = self.confirm(self.dispatch(self.vendor_payout(50_000, self.lekki_bank)))

        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(held.held_balance(self.lekki.pk), 148_000 - 50_000 - FEE)
        self.fake.balances["NGN"] = 148_000 - 50_000 - FEE
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees), (0, True))
