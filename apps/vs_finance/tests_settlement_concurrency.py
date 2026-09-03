"""Settling one document twice at once must not credit AR twice.

Settlement is a read-modify-write. It reads ``balance_due`` (derived from the
stored ``amount_paid``), decides how much of the cash to apply, and writes the
new total back. ``@transaction.atomic`` makes that atomic; on PostgreSQL's
default READ COMMITTED it does not make it isolated, so two settlements running
together read the same pre-update balance and the second write discards the
first.

The damage is not a stale number. Each run also writes its own allocation row and
posts its own journal crediting AR, so afterwards the sub-ledger holds two
settlements, the general ledger has credited AR twice, and the invoice reports
being paid once. The AR control account is left carrying a credit the trial
balance cannot explain - the exact state ``_post_payment_atomic``'s "split at
source" design exists to make impossible.

This is an ordinary Monday at a school, not a contrived race: a bursar recording
a counter receipt while the Paystack webhook for the same invoice is confirming
is two concurrent settlements of one document, and both reach
``_apply_payment_subledger``.

These tests hold the first transaction open inside the lock and start the second
against it, so they fail without the fix rather than passing by winning a
timing coin-toss. The invariant asserted is the one the bug breaks and the one
worth protecting whatever the implementation: **a document's ``amount_paid``
always equals the sum of the allocation rows pointing at it.**
"""
from __future__ import annotations

import datetime
import threading
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase

from vs_finance import receivables as receivables_services
from vs_finance.constants import DocumentStatus, InvoicePaymentStatus
from vs_finance.models import (
    Account,
    Invoice,
    Payment,
    PaymentAllocation,
)
from vs_finance.receivables import post_invoice, post_payment

from .tests import _ARFixtureMixin


class SettlementConcurrencyTests(_ARFixtureMixin, TransactionTestCase):
    """Two receipts, one invoice, at the same moment."""

    serialized_rollback = True

    INVOICE_KOBO = 450_000

    def build_invoice(self):
        entity, period, customer, _vat = self.build_ar()
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, self.INVOICE_KOBO, None)],
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, self.INVOICE_KOBO)
        return entity, period, customer, invoice

    def draft_receipt(self, entity, customer, amount, reference):
        return Payment.objects.create(
            entity=entity, customer=customer,
            payment_date=datetime.date(2026, 1, 20),
            method="BANK_TRANSFER", amount=amount,
            deposit_account=Account.objects.get(entity=entity, code="1100"),
            reference=reference,
        )

    def race_two_receipts(self, entity, customer, invoice, *, explicit):
        """Post two full-balance receipts against *invoice* concurrently.

        The first is held inside its transaction - after the row lock is taken
        and the allocation written, before commit - until the second is confirmed
        to be in flight against it. Without that gate the two would usually
        serialise by luck and the test would pass on buggy code.
        """
        first_holding = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        outcomes = {}
        real_stamp = receivables_services.stamp_allocation_effective_date

        def blocking_stamp(rows, effective):
            # Called from inside the settlement transaction, so the lock this
            # test is about is definitely held by the time we block here.
            result = real_stamp(rows, effective)
            if not first_holding.is_set():
                first_holding.set()
                if not release_first.wait(10):
                    raise TimeoutError("settlement race did not release the first run")
            return result

        def worker(name, payment_id, *, started=None):
            close_old_connections()
            try:
                if started:
                    started.set()
                payment = Payment.objects.get(pk=payment_id)
                # Each thread loads its **own** Invoice instance, exactly as two
                # HTTP requests in two processes would. Sharing one Python object
                # lets the first run's in-memory ``amount_paid`` update reach the
                # second for free, and the test then passes against unlocked
                # code, proving nothing.
                mine = Invoice.objects.get(pk=invoice.pk)
                allocations = [(mine, self.INVOICE_KOBO)] if explicit else None
                post_payment(
                    payment, allocations=allocations,
                    auto_allocate=not explicit,
                )
                outcomes[name] = "posted"
            except Exception as exc:  # recorded, then asserted on by the caller
                outcomes[f"{name}_error"] = exc
            finally:
                close_old_connections()

        first_payment = self.draft_receipt(entity, customer, self.INVOICE_KOBO, "RACE-1")
        second_payment = self.draft_receipt(entity, customer, self.INVOICE_KOBO, "RACE-2")

        with patch.object(
            receivables_services, "stamp_allocation_effective_date", blocking_stamp,
        ):
            first = threading.Thread(
                target=worker, args=("first", first_payment.pk), daemon=True,
            )
            second = threading.Thread(
                target=worker, args=("second", second_payment.pk),
                kwargs={"started": second_started}, daemon=True,
            )
            first.start()
            try:
                self.assertTrue(
                    first_holding.wait(10), "the first receipt never reached the lock",
                )
                second.start()
                self.assertTrue(second_started.wait(10))
                # Give the second run time to reach the lock and block on it.
                second.join(1.0)
            finally:
                release_first.set()
            first.join(15)
            second.join(15)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        return outcomes

    # -- the invariant --------------------------------------------------------- #

    def assert_subledger_agrees(self, invoice):
        """``amount_paid`` is the sum of the allocations, and never exceeds the total.

        Asserted rather than the narrower "only one allocation exists", because
        the rule has to hold however the second receipt is handled - whether it
        applies nothing, or applies a part balance left by a partial first run.
        """
        invoice.refresh_from_db()
        allocated = sum(
            PaymentAllocation.objects.filter(invoice=invoice)
            .values_list("amount", flat=True)
        )
        self.assertEqual(
            invoice.amount_paid, allocated,
            "the invoice's paid total must equal the allocation rows pointing at it",
        )
        self.assertLessEqual(
            invoice.amount_paid, invoice.total,
            "an invoice can never be settled for more than it is worth",
        )

    def test_two_explicit_receipts_cannot_both_settle_one_invoice(self):
        """The named-allocation path: both callers say "apply 450,000 to this invoice"."""
        entity, _period, customer, invoice = self.build_invoice()

        outcomes = self.race_two_receipts(
            entity, customer, invoice, explicit=True,
        )

        self.assertNotIn("first_error", outcomes, outcomes.get("first_error"))
        self.assert_subledger_agrees(invoice)

    def test_two_auto_allocated_receipts_cannot_both_settle_one_invoice(self):
        """The auto-allocation path, which is what the counter receipt screen uses."""
        entity, _period, customer, invoice = self.build_invoice()

        outcomes = self.race_two_receipts(
            entity, customer, invoice, explicit=False,
        )

        self.assertNotIn("first_error", outcomes, outcomes.get("first_error"))
        self.assert_subledger_agrees(invoice)

    def test_the_second_receipts_money_is_kept_as_customer_credit(self):
        """Losing the race must not lose the cash.

        The parent's second payment is real money that arrived. It cannot settle
        an invoice that is already paid, so it has to land in the customer-credit
        liability instead - which is what makes refusing the double settlement
        safe rather than merely tidy.
        """
        entity, _period, customer, invoice = self.build_invoice()

        self.race_two_receipts(entity, customer, invoice, explicit=False)

        self.assert_subledger_agrees(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

        posted = Payment.objects.filter(
            entity=entity, customer=customer, status=DocumentStatus.POSTED,
        )
        self.assertEqual(posted.count(), 2, "both receipts are still recorded")
        self.assertEqual(
            sum(posted.values_list("amount", flat=True)), self.INVOICE_KOBO * 2,
            "both receipts kept their full amount",
        )
        self.assertEqual(
            sum(posted.values_list("allocated_amount", flat=True)), self.INVOICE_KOBO,
            "but only one invoice's worth was ever allocated",
        )

    def test_ar_is_credited_exactly_once_for_the_invoice(self):
        """The general ledger half of the same invariant.

        The sub-ledger check above would still pass if the journals had drifted,
        so the AR control account is asserted directly: it is debited once by the
        invoice and credited once by the settlement, netting to nil, with the
        second receipt's cash sitting in customer credit rather than in AR.
        """
        entity, period, customer, invoice = self.build_invoice()

        self.race_two_receipts(entity, customer, invoice, explicit=False)

        from vs_finance.models import AccountBalance

        ar = AccountBalance.objects.get(
            account__entity=entity, account__code="1200", period=period,
        )
        self.assertEqual(
            ar.debit_total - ar.credit_total, 0,
            "AR must net to nil: one invoice raised, one settlement applied",
        )
        self.assertEqual(ar.credit_total, self.INVOICE_KOBO)
