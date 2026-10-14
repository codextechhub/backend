"""The provider's transfer fee is reserved with, and booked beside, every payout the branch makes.

Corona's Lekki Branch holds N178,000 for Mrs Adeyemi's online payment. It pays a
printer N50,000 online and Paystack charges N10 to send it. The branch bears the
fee: N50,010 leaves Lekki's held balance when the transfer is sent, comes back
if the transfer fails, and once it is paid Lekki's books show the N10 as a bank
charge taken from gateway clearing. Paystack's balance fell by the same N50,010,
so the daily held-ledger check balances. Before, only a settlement carried its
fee, and every supplier paid online left the check N10 out.
"""
from __future__ import annotations

from unittest.mock import patch

from vs_finance.models import JournalLine

from . import held, held_reconciliation
from .constants import HeldMovementKind, PayoutStatus
from .models import HeldMovement
from .tests_custody_held import _HeldFixture

FEE = 1_000


class VendorTransferFeeTests(_HeldFixture):

    def setUp(self):
        super().setUp()
        self.fake.transfer_fee_kobo = FEE
        notify = patch.object(held_reconciliation, "_notify_operators", return_value=1)
        notify.start()
        self.addCleanup(notify.stop)

    def fee_lines(self, payout):
        """The journal lines of the fee booking for ``payout``, as ``(account code, debit, credit)``."""
        return sorted(
            (line.account.code, line.debit, line.credit)
            for line in JournalLine.objects.filter(
                entry__entity=self.books, entry__reference=payout.reference,
                description__in=("Transfer fee", "Gateway clearing"),
            ).select_related("account"))

    def test_the_fee_is_reserved_with_the_amount_and_recorded_on_the_payout(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        payout = self.dispatch(self.vendor_payout(50_000, self.lekki_bank))

        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(payout.metadata["transfer_fee"], FEE)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000 - 50_000 - FEE)
        movement = HeldMovement.objects.get(payout=payout, kind=HeldMovementKind.PAYOUT)
        self.assertEqual(movement.amount, -(50_000 + FEE))
        self.assert_books_agree()

    def test_a_payout_the_balance_covers_only_without_its_fee_is_refused(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)  # 178,000 held.
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            payout = self.dispatch(self.vendor_payout(178_000 - FEE + 1, self.lekki_bank))
        sent.assert_not_called()
        self.assertEqual(payout.status, PayoutStatus.FAILED)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_paid_payout_books_the_fee_to_bank_charges_out_of_clearing_once(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        payout = self.confirm(self.dispatch(self.vendor_payout(50_000, self.lekki_bank)))

        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(self.fee_lines(payout), [("1125", 0, FEE), ("5500", FEE, 0)])
        entry = JournalLine.objects.get(
            entry__reference=payout.reference, account__code="5500").entry
        self.assertEqual(entry.branch_id, self.lekki.pk)
        # Lekki's clearing held the whole N180,000 payment; the supplier's N50,000
        # and the N10 fee both left it.
        self.assertEqual(
            self.balance(self.clearing(self.books), self.lekki), 180_000 - 50_000 - FEE)
        self.confirm_again(payout)
        self.assertEqual(len(self.fee_lines(payout)), 2)

    def confirm_again(self, payout):
        """A repeated confirmation books nothing more."""
        from . import services

        services.confirm_payout(payout)

    def test_a_failed_payout_gives_back_the_amount_and_the_fee_and_books_no_fee(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        payout = self.dispatch(self.vendor_payout(50_000, self.lekki_bank))
        self.confirm(payout, status=PayoutStatus.FAILED)

        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        release = HeldMovement.objects.get(payout=payout, kind=HeldMovementKind.RELEASE)
        self.assertEqual(release.amount, 50_000 + FEE)
        self.assertEqual(self.fee_lines(payout), [])
        self.assert_books_agree()

    def test_a_vendor_payout_then_the_held_reconciliation_balances(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        self.confirm(self.dispatch(self.vendor_payout(50_000, self.lekki_bank)))

        # Paystack held 178,000 + 39,400; the supplier's transfer took 50,000 and its fee.
        self.fake.balances["NGN"] = 178_000 + 39_400 - 50_000 - FEE
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees), (0, True))

    def test_without_the_fee_the_check_would_have_been_out_by_it(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.confirm(self.dispatch(self.vendor_payout(50_000, self.lekki_bank)))
        self.fake.balances["NGN"] = 178_000 - 50_000  # Paystack that forgot its fee.
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees), (FEE, False))
