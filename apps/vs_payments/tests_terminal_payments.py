"""A payment on a branch terminal is booked on arrival and matched to a customer afterwards.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch. Mrs Okeye pays
N150,000 at Lekki's terminal. The provider says only which terminal, how much,
her email and her name as her bank has it. The money is real, so Lekki's books
take it at once: Dr gateway clearing, Cr unidentified receipts, and the platform
holds it for Lekki like a checkout. If exactly one Lekki customer has her email
the payment is applied to them at once, a part payment against the oldest bill
first. If two children share her email, or nobody has it, the payment waits on
Lekki's unmatched list with up to five suggestions; a bursar chooses a Lekki
customer (a Lekki payment never goes to an Ikeja family), and can take the match
back. An Ikeja clerk never sees Lekki's payments.
"""
from __future__ import annotations

import datetime
import itertools
import json
from unittest.mock import patch

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.constants import DocumentStatus
from vs_finance.exceptions import PostingError
from vs_finance.models import Account, Customer, Invoice, JournalEntry, Payment
from vs_finance.receivables import post_invoice
from vs_rbac.field_enforcement import can_read
from vs_rbac.field_evaluator import get_field_access
from vs_rbac.field_registry import get_declaration
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from . import held, held_reconciliation, terminal_payments, terminals, webhooks
from .constants import (
    CollectionStatus,
    PaymentAuditAction,
    TerminalMatchMethod,
    TerminalPaymentStatus,
    WebhookStatus,
)
from .exceptions import PaymentStateError
from .models import (
    CollectionIntent,
    HeldMovement,
    PaymentEvent,
    TerminalPayment,
    VirtualTerminal,
)
from .tests_terminals import _TerminalFixture

_refs = itertools.count(1)
_people = itertools.count(1)


class _PaymentFixture(_TerminalFixture):
    """Corona's Lekki terminal open, with families the matching can choose between."""

    @classmethod
    def person(cls, entity, code, name, email, branch):
        customer = cls.customer(entity, code, branch)
        Customer.objects.filter(pk=customer.pk).update(name=name, billing_email=email)
        customer.refresh_from_db()
        return customer

    @classmethod
    def bill(cls, entity, customer, branch, *, invoice_date=None, due_date=None, amount=100_000):
        invoice = cls.invoice(entity, customer, branch)
        changes = {key: value for key, value in (
            ("invoice_date", invoice_date), ("due_date", due_date)) if value}
        if changes:
            Invoice.objects.filter(pk=invoice.pk).update(**changes)
        invoice.lines.update(unit_price=amount, revenue_account=Account.objects.get(
            entity=entity, code="4100"))
        invoice.refresh_from_db()
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        books, lekki, ikeja = cls.books, cls.lekki, cls.ikeja
        cls.ada = cls.person(books, "CADA", "Ada Okeye", "Ada.Okeye@Example.com", lekki)
        cls.tunde = cls.person(books, "CTUN", "Tunde Okeye", "family@example.com", lekki)
        cls.bola = cls.person(books, "CBOL", "Bola Okeye", "family@example.com", lekki)
        cls.ikeja_ada = cls.person(books, "CIKA", "Ada Okeye", "ada.okeye@example.com", ikeja)
        cls.chidi = cls.person(books, "CSHR", "Chidi Shared", "shared@example.com", None)
        cls.gone = cls.person(books, "CGON", "Gone Family", "gone@example.com", lekki)
        Customer.objects.filter(pk=cls.gone.pk).update(is_active=False)
        Customer.objects.filter(pk=cls.solo_parent.pk).update(
            name="Solo Parent", billing_email="solo@example.com")
        cls.rival_parent = cls.person(
            cls.rival_books, "CRIV", "Rival Parent", "rival@example.com", cls.rival_branch)
        cls.ada_first = cls.bill(books, cls.ada, lekki)
        cls.ada_second = cls.bill(
            books, cls.ada, lekki, invoice_date=datetime.date(2026, 2, 10),
            due_date=datetime.date(2026, 3, 1))

    def setUp(self):
        super().setUp()
        self.lekki_terminal = self.open(self.lekki)
        self.ikeja_terminal = self.open(self.ikeja)
        self.solo_terminal = self.open(self.solo_main)

    # -- helpers --------------------------------------------------------------- #

    def pay(self, terminal, amount=150_000, *, email="", sender="", bank="", fee=2_000):
        """Deliver a signed terminal event and process it; the terminal payment it made."""
        reference = f"TRM{next(_refs):07d}"
        self.fake.forced_status[reference] = CollectionStatus.SUCCEEDED
        self.fake.forced_amount[reference] = amount
        self.fake.forced_fee[reference] = fee
        body, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="success", amount=amount,
            terminal_code=terminal.code, payer_email=email, sender_name=sender, sender_bank=bank,
            provider_id=str(next(_refs) + 1000))
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
        webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.last_event = event
        return TerminalPayment.objects.select_related("collection").get(collection__reference=reference)

    def suspense(self, entity, branch=None):
        """What unidentified receipts holds, as a positive amount of what is owed."""
        return -self.balance(Account.objects.get(entity=entity, code="2195"), branch)

    def receivable(self, entity, branch=None):
        return self.balance(Account.objects.get(entity=entity, code="1200"), branch)

    def credit(self, entity, branch=None):
        return -self.balance(Account.objects.get(entity=entity, code="2140"), branch)

    def amounts_paid(self, *invoices):
        return [Invoice.objects.get(pk=invoice.pk).amount_paid for invoice in invoices]

    def settle_run(self, run):
        payout = self.dispatch(run.batch)
        return self.confirm(payout, amount=run.amount)


class TerminalPaymentBookingTests(_PaymentFixture):
    """The money is booked when the provider confirms it, whoever it turns out to be from."""

    def test_a_payment_nobody_can_name_is_booked_to_unidentified_receipts(self):
        tp = self.pay(self.lekki_terminal, 180_000, email="stranger@example.com",
                      sender="C BELLO", bank="GTBank", fee=2_000)
        intent = tp.collection

        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual((intent.channel, intent.branch_id, intent.fee, intent.amount),
                         ("TERMINAL", self.lekki.pk, 2_000, 180_000))
        self.assertEqual((tp.branch_id, tp.terminal_id, tp.payer_email, tp.sender_name, tp.sender_bank),
                         (self.lekki.pk, self.lekki_terminal.pk, "stranger@example.com",
                          "C BELLO", "GTBank"))
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 180_000)
        self.assertEqual(self.suspense(self.books, self.lekki), 180_000)
        journal = tp.suspense_journal
        self.assertEqual((journal.status, journal.branch_id, journal.entity_id),
                         (DocumentStatus.POSTED, self.lekki.pk, self.books.pk))
        self.assertEqual(sorted((line.account.code, line.debit, line.credit) for line in journal.lines.all()),
                         [("1125", 180_000, 0), ("2195", 0, 180_000)])
        self.assertFalse(Payment.objects.filter(reference=intent.reference).exists())
        self.assertIsNone(intent.customer_id)
        self.assertIn("No customer of this branch has the payer's email", tp.match_note)
        self.assertTrue(intent.awaits_settlement)
        self.assertEqual(intent.received_on, tenant_today(self.tenant))
        self.assertEqual(self.last_event.status, WebhookStatus.PROCESSED)
        self.assertEqual(
            PaymentEvent.objects.filter(action=PaymentAuditAction.TERMINAL_PAYMENT_RECEIVED,
                                        reference=intent.reference).count(), 1)

    def test_the_platform_holds_it_for_the_branch_like_a_checkout(self):
        tp = self.pay(self.lekki_terminal, 180_000, email="stranger@example.com", fee=2_000)

        self.assertTrue(tp.collection.held_by_platform)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        movement = HeldMovement.objects.get(collection=tp.collection)
        self.assertEqual((movement.amount, movement.branch_id), (178_000, self.lekki.pk))
        self.assertEqual(movement.platform_journal.status, DocumentStatus.POSTED)
        self.assert_books_agree()

    def test_the_held_ledger_check_still_balances_after_a_terminal_payment(self):
        self.pay(self.lekki_terminal, 180_000, email="stranger@example.com", fee=2_000)
        self.pay(self.lekki_terminal, 150_000, email="ada.okeye@example.com", fee=1_500)
        self.pay(self.solo_terminal, 40_000, email="solo@example.com", fee=600)
        self.fake.balances["NGN"] = 178_000 + 148_500 + 39_400
        with patch.object(held_reconciliation, "_notify_operators", return_value=1):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertTrue(row.agrees, row)
        self.assertEqual(row.held_total, 178_000 + 148_500 + 39_400)

    def test_a_payment_with_no_email_waits_with_a_reason(self):
        tp = self.pay(self.lekki_terminal, 20_000, email="", sender="Someone")
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)
        self.assertIn("did not report the payer's email", tp.match_note)

    def test_a_redelivered_event_books_nothing_twice(self):
        tp = self.pay(self.lekki_terminal, 90_000, email="stranger@example.com")
        journals = JournalEntry.objects.filter(entity=self.books).count()
        webhooks.process_stored_event(self.last_event.pk)
        replay = webhooks.process_stored_event(self.last_event.pk)
        self.assertEqual(replay.status, WebhookStatus.PROCESSED)
        self.assertEqual(TerminalPayment.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(entity=self.books).count(), journals)
        self.assertEqual(self.suspense(self.books, self.lekki), 90_000)

    def test_an_event_naming_an_unknown_terminal_books_nothing(self):
        reference = "TRMUNKNOWN"
        body, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="success", amount=50_000,
            terminal_code="VT_NOT_OURS", payer_email="x@example.com")
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
        webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookStatus.IGNORED)
        self.assertIn("VT_NOT_OURS", event.error)
        self.assertFalse(CollectionIntent.objects.filter(reference=reference).exists())

    def test_the_amount_comes_from_the_provider_not_the_event(self):
        reference = "TRMLIAR"
        self.fake.forced_status[reference] = CollectionStatus.SUCCEEDED
        self.fake.forced_amount[reference] = 10_000
        body, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="success", amount=9_000_000,
            terminal_code=self.lekki_terminal.code, payer_email="x@example.com")
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
        webhooks.process_stored_event(event.pk)
        self.assertEqual(CollectionIntent.objects.get(reference=reference).amount, 10_000)
        self.assertEqual(self.suspense(self.books, self.lekki), 10_000)

    def test_a_provider_that_reports_no_amount_leaves_it_unbooked_for_review(self):
        reference = "TRMNOAMT"
        self.fake.forced_status[reference] = CollectionStatus.SUCCEEDED
        body, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="success", amount=50_000,
            terminal_code=self.lekki_terminal.code, payer_email="x@example.com")
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
        webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookStatus.FAILED)
        self.assertEqual(self.suspense(self.books, self.lekki), 0)
        self.assertIsNone(TerminalPayment.objects.get(collection__reference=reference).suspense_journal)

    def test_a_verification_naming_another_terminal_is_not_booked(self):
        from .providers.base import CollectionStatusResult

        reference = "TRMSWAP"
        result = CollectionStatusResult(
            reference=reference, status="SUCCEEDED", amount=50_000,
            raw={"metadata": {"virtual_terminal": {"code": self.ikeja_terminal.code}}})
        body, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="success", amount=50_000,
            terminal_code=self.lekki_terminal.code, payer_email="x@example.com")
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
        with patch.object(self.fake, "verify_collection", return_value=result):
            webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookStatus.FAILED)
        self.assertIn("different terminal", event.error)
        self.assertEqual(self.suspense(self.books), 0)

    def test_a_tenant_with_no_books_is_left_unresolved(self):
        from vs_finance.models import LedgerEntity

        LedgerEntity.objects.filter(pk=self.solo_books.pk).update(is_active=False)
        body, headers = self.fake.build_webhook(
            event="charge.success", reference="TRMNOBOOKS", status="success", amount=50_000,
            terminal_code=self.solo_terminal.code, payer_email="x@example.com")
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
        webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookStatus.IGNORED)


class AutomaticMatchTests(_PaymentFixture):
    """Exactly one active customer of the branch with the payer's email gets the payment."""

    def test_one_customer_with_the_email_is_applied_oldest_bill_first_as_a_part_payment(self):
        tp = self.pay(self.lekki_terminal, 150_000, email="  ADA.okeye@EXAMPLE.com ",
                      sender="ADA OKEYE", fee=1_500)

        self.assertEqual((tp.status, tp.match_method, tp.customer_id),
                         (TerminalPaymentStatus.MATCHED, TerminalMatchMethod.AUTO, self.ada.pk))
        self.assertIsNone(tp.matched_by_id)
        receipt = tp.match_payment
        self.assertEqual((receipt.status, receipt.customer_id, receipt.branch_id, receipt.amount),
                         (DocumentStatus.POSTED, self.ada.pk, self.lekki.pk, 150_000))
        self.assertEqual(receipt.deposit_account.code, "2195")
        self.assertEqual(self.amounts_paid(self.ada_first, self.ada_second), [100_000, 50_000])
        self.assertEqual((tp.collection.customer_id, tp.collection.payment_id),
                         (self.ada.pk, receipt.pk))
        self.assertEqual(self.suspense(self.books, self.lekki), 0)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 150_000)
        self.assertEqual(self.receivable(self.books, self.lekki), 50_000)
        self.assertTrue(tp.collection.awaits_settlement)
        event = PaymentEvent.objects.get(action=PaymentAuditAction.TERMINAL_PAYMENT_MATCHED)
        self.assertEqual((event.metadata["method"], event.metadata["customer_id"]),
                         ("AUTO", self.ada.pk))

    def test_a_payment_larger_than_the_bills_leaves_the_rest_as_credit(self):
        tp = self.pay(self.lekki_terminal, 250_000, email="ada.okeye@example.com")
        self.assertEqual(self.amounts_paid(self.ada_first, self.ada_second), [100_000, 100_000])
        self.assertEqual(tp.match_payment.allocated_amount, 200_000)
        self.assertEqual(self.credit(self.books, self.lekki), 50_000)
        self.assertEqual(self.suspense(self.books, self.lekki), 0)

    def test_two_customers_sharing_the_email_leave_it_for_a_person(self):
        tp = self.pay(self.lekki_terminal, 60_000, email="family@example.com")
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)
        self.assertIn("More than one customer has the payer's email", tp.match_note)
        self.assertEqual(self.suspense(self.books, self.lekki), 60_000)
        self.assertFalse(Payment.objects.filter(reference=tp.collection.reference).exists())

    def test_an_unknown_email_leaves_it_for_a_person(self):
        tp = self.pay(self.lekki_terminal, 60_000, email="nobody@example.com")
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)

    def test_another_branchs_customer_with_the_email_is_not_matched(self):
        # CIKA (Ikeja) has the same email as Ada, but a Lekki payment is not an Ikeja family's.
        lekki = self.pay(self.lekki_terminal, 10_000, email="ada.okeye@example.com")
        self.assertEqual(lekki.customer_id, self.ada.pk)
        ikeja = self.pay(self.ikeja_terminal, 10_000, email="ada.okeye@example.com")
        self.assertEqual(ikeja.customer_id, self.ikeja_ada.pk)
        stranger = self.pay(self.ikeja_terminal, 10_000, email="family@example.com")
        self.assertEqual(stranger.status, TerminalPaymentStatus.UNMATCHED)

    def test_an_inactive_customer_is_not_matched(self):
        tp = self.pay(self.lekki_terminal, 10_000, email="gone@example.com")
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)

    def test_a_customer_every_branch_shares_is_matched_to_any_branchs_payment(self):
        tp = self.pay(self.ikeja_terminal, 25_000, email="shared@example.com")
        self.assertEqual((tp.status, tp.customer_id), (TerminalPaymentStatus.MATCHED, self.chidi.pk))
        self.assertEqual(tp.match_payment.branch_id, self.ikeja.pk)
        self.assertEqual(self.credit(self.books, self.ikeja), 25_000)

    def test_a_one_branch_tenant_matches_the_same_way(self):
        bill = self.bill(self.solo_books, self.solo_parent, self.solo_main, amount=40_000)
        tp = self.pay(self.solo_terminal, 25_000, email="solo@example.com", fee=400)
        self.assertEqual((tp.status, tp.customer_id, tp.branch_id),
                         (TerminalPaymentStatus.MATCHED, self.solo_parent.pk, self.solo_main.pk))
        self.assertEqual(self.amounts_paid(bill), [25_000])
        self.assertEqual(self.suspense(self.solo_books), 0)
        self.assertEqual(held.held_balance(self.solo_main.pk), 24_600)

    def test_a_match_that_cannot_post_leaves_the_money_booked_and_unmatched(self):
        with patch("vs_finance.receivables.post_payment", side_effect=PostingError("The period is closed.")):
            tp = self.pay(self.lekki_terminal, 150_000, email="ada.okeye@example.com")
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)
        self.assertIn("The period is closed", tp.match_note)
        self.assertEqual(tp.collection.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(self.suspense(self.books, self.lekki), 150_000)
        self.assertEqual(Payment.objects.filter(reference=tp.collection.reference).count(), 0)
        self.assertEqual(held.held_balance(self.lekki.pk), 148_000)


class ManualMatchTests(_PaymentFixture):
    def setUp(self):
        super().setUp()
        self.bursar = self.user_for(self.tenant, f"bursar{next(_people)}@corona.test")

    def waiting(self, amount=150_000, **kwargs):
        return self.pay(self.lekki_terminal, amount, email="stranger@example.com", **kwargs)

    def test_a_person_applies_it_to_a_customer_of_the_branch(self):
        tp = self.waiting()

        matched = terminal_payments.match(tp, self.ada, actor_user=self.bursar)

        matched.refresh_from_db()
        self.assertEqual((matched.status, matched.match_method, matched.customer_id, matched.matched_by_id),
                         (TerminalPaymentStatus.MATCHED, TerminalMatchMethod.MANUAL, self.ada.pk,
                          self.bursar.pk))
        self.assertIsNotNone(matched.matched_at)
        receipt = matched.match_payment
        self.assertEqual((receipt.deposit_account.code, receipt.branch_id, receipt.payment_date),
                         ("2195", self.lekki.pk, tenant_today(self.tenant)))
        lines = sorted((line.account.code, line.debit, line.credit) for line in receipt.journal.lines.all())
        self.assertEqual(lines, [("1200", 0, 150_000), ("2195", 150_000, 0)])  # Dr suspense, Cr receivable.
        self.assertEqual(self.amounts_paid(self.ada_first, self.ada_second), [100_000, 50_000])
        self.assertEqual(self.suspense(self.books, self.lekki), 0)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 150_000)
        self.assertEqual(matched.collection.customer_id, self.ada.pk)
        self.assertEqual(
            PaymentEvent.objects.filter(action=PaymentAuditAction.TERMINAL_PAYMENT_MATCHED).get()
            .actor_user_id, self.bursar.pk)

    def test_a_customer_of_another_branch_is_refused_and_nothing_is_booked(self):
        tp = self.waiting()
        receipts = Payment.objects.count()
        with self.assertRaises(ValidationError) as caught:
            terminal_payments.match(tp, self.okafor, actor_user=self.bursar)
        self.assertIn("another branch than Lekki Branch", str(caught.exception.detail["customer"]))
        self.assertEqual(Payment.objects.count(), receipts)
        self.assertEqual(self.suspense(self.books, self.lekki), 150_000)
        tp.refresh_from_db()
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)

    def test_a_customer_of_another_tenant_is_refused(self):
        tp = self.waiting()
        with self.assertRaises(ValidationError):
            terminal_payments.match(tp, self.rival_parent, actor_user=self.bursar)

    def test_a_customer_every_branch_shares_may_be_chosen(self):
        tp = self.waiting(20_000)
        terminal_payments.match(tp, self.chidi, actor_user=self.bursar)
        self.assertEqual(self.credit(self.books, self.lekki), 20_000)

    def test_a_matched_payment_cannot_be_matched_again(self):
        tp = self.waiting()
        terminal_payments.match(tp, self.ada, actor_user=self.bursar)
        with self.assertRaises(PaymentStateError):
            terminal_payments.match(tp, self.tunde, actor_user=self.bursar)

    def test_a_payment_not_yet_booked_cannot_be_matched(self):
        intent, _ = terminal_payments.record_terminal_payment(
            terminal=self.lekki_terminal, reference="TRMRAW", amount=1_000)
        with self.assertRaises(PaymentStateError):
            terminal_payments.match(intent.terminal_payment, self.ada, actor_user=self.bursar)

    def test_unmatching_reverses_the_receipt_and_puts_the_payment_back(self):
        tp = self.waiting()
        terminal_payments.match(tp, self.ada, actor_user=self.bursar)
        receipt = Payment.objects.get(reference=tp.collection.reference, status=DocumentStatus.POSTED)

        terminal_payments.unmatch(tp, actor_user=self.bursar)

        tp.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual((tp.status, tp.customer_id, tp.match_payment_id, tp.match_method),
                         (TerminalPaymentStatus.UNMATCHED, None, None, ""))
        self.assertEqual((tp.unmatched_by_id, receipt.status), (self.bursar.pk, DocumentStatus.REVERSED))
        self.assertIsNotNone(tp.unmatched_at)
        self.assertEqual(self.amounts_paid(self.ada_first, self.ada_second), [0, 0])
        self.assertEqual(self.suspense(self.books, self.lekki), 150_000)
        self.assertEqual(self.receivable(self.books, self.lekki), 200_000)
        tp.collection.refresh_from_db()
        self.assertEqual((tp.collection.customer_id, tp.collection.payment_id), (None, None))
        self.assertTrue(tp.collection.awaits_settlement)
        self.assertEqual(
            PaymentEvent.objects.filter(action=PaymentAuditAction.TERMINAL_PAYMENT_UNMATCHED).count(), 1)

        again = terminal_payments.match(tp, self.tunde, actor_user=self.bursar)  # Matching is possible again.
        self.assertEqual(again.customer_id, self.tunde.pk)
        self.assertEqual(self.suspense(self.books, self.lekki), 0)

    def test_an_automatic_match_can_be_unmatched_too(self):
        tp = self.pay(self.lekki_terminal, 150_000, email="ada.okeye@example.com")
        terminal_payments.unmatch(tp, actor_user=self.bursar)
        tp.refresh_from_db()
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)
        self.assertEqual(self.suspense(self.books, self.lekki), 150_000)
        self.assertEqual(self.amounts_paid(self.ada_first, self.ada_second), [0, 0])

    def test_unmatching_a_payment_that_is_not_matched_is_refused(self):
        tp = self.waiting()
        with self.assertRaises(PaymentStateError):
            terminal_payments.unmatch(tp, actor_user=self.bursar)

    def test_a_receipt_finance_cannot_void_leaves_the_match_standing(self):
        tp = self.waiting()
        terminal_payments.match(tp, self.ada, actor_user=self.bursar)
        with patch("vs_finance.voids.void_payment", side_effect=PostingError("Credit was refunded.")):
            with self.assertRaises(PostingError):
                terminal_payments.unmatch(tp, actor_user=self.bursar)
        tp.refresh_from_db()
        self.assertEqual((tp.status, tp.customer_id), (TerminalPaymentStatus.MATCHED, self.ada.pk))
        self.assertEqual(self.suspense(self.books, self.lekki), 0)

    def test_a_matching_receipt_voided_on_the_receipts_screen_puts_the_payment_back(self):
        from vs_finance.voids import void_payment

        tp = self.waiting()
        matched = terminal_payments.match(tp, self.ada, actor_user=self.bursar)
        void_payment(matched.match_payment, actor_user=self.bursar)
        tp = TerminalPayment.objects.get(pk=tp.pk)
        self.assertTrue(terminal_payments.is_open(tp))
        self.assertTrue(TerminalPayment.objects.filter(terminal_payments.open_payments_q(), pk=tp.pk).exists())
        self.assertEqual(self.suspense(self.books, self.lekki), 150_000)
        terminal_payments.match(tp, self.tunde, actor_user=self.bursar)
        self.assertEqual(self.suspense(self.books, self.lekki), 0)


class SuggestionTests(_PaymentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dayo = cls.person(cls.books, "CDAY", "Dayo Balogun", "dayo@example.com", cls.lekki)
        cls.bill(cls.books, cls.dayo, cls.lekki)  # One open bill of exactly N100,000.

    def codes(self, tp):
        return [row["code"] for row in terminal_payments.suggestions(tp)]

    def test_email_first_then_a_name_like_the_senders_then_an_open_balance_equal_to_the_amount(self):
        tp = self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE")
        rows = terminal_payments.suggestions(tp)

        self.assertEqual([row["code"] for row in rows], ["CBOL", "CTUN", "CADA", "CDAY"])
        by_code = {row["code"]: row for row in rows}
        self.assertTrue(by_code["CTUN"]["email_match"] and by_code["CBOL"]["email_match"])
        self.assertIn("email", by_code["CTUN"]["reasons"])
        self.assertEqual(by_code["CADA"]["reasons"], ["name"])
        self.assertFalse(by_code["CADA"]["email_match"])
        self.assertEqual(by_code["CDAY"]["reasons"], ["amount"])
        self.assertEqual((by_code["CDAY"]["open_balance"], by_code["CADA"]["open_balance"]),
                         (100_000, 200_000))

    def test_the_email_local_part_stands_in_for_a_missing_sender(self):
        self.person(self.books, "CFOL", "Folasade Adewale", "", self.lekki)
        tp = self.pay(self.lekki_terminal, 33_000, email="folasade.adewale@elsewhere.com", sender="")
        self.assertEqual(self.codes(tp), ["CFOL"])

    def test_at_most_five_are_suggested(self):
        for number in range(7):
            self.person(self.books, f"CSIB{number}", f"Sibling {number}", "many@example.com", self.lekki)
        tp = self.pay(self.lekki_terminal, 33_000, email="many@example.com")
        self.assertEqual(len(terminal_payments.suggestions(tp)), terminal_payments.SUGGESTION_LIMIT)

    def test_it_never_offers_another_branchs_customer_or_an_inactive_one(self):
        tp = self.pay(self.lekki_terminal, 33_000, email="ada.okeye@example.com", sender="ADA OKEYE")
        # Ada is matched automatically; look at the list as it would be had she not been.
        terminal_payments.unmatch(tp)
        codes = self.codes(TerminalPayment.objects.get(pk=tp.pk))
        self.assertIn("CADA", codes)
        self.assertNotIn("CIKA", codes)
        gone = self.pay(self.lekki_terminal, 33_000, email="gone@example.com", sender="Gone Family")
        self.assertNotIn("CGON", self.codes(gone))

    def test_a_customer_every_branch_shares_can_be_suggested(self):
        tp = self.pay(self.lekki_terminal, 33_000, email="x@example.com", sender="Chidi Shared")
        self.assertEqual(self.codes(tp), ["CSHR"])

    def test_nothing_resembles_an_unknown_payer(self):
        tp = self.pay(self.lekki_terminal, 33_000, email="q@example.com", sender="Zzz Yyy")
        self.assertEqual(terminal_payments.suggestions(tp), [])

    def test_the_ranking_is_the_same_every_time(self):
        tp = self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE")
        self.assertEqual(terminal_payments.suggestions(tp), terminal_payments.suggestions(tp))


class SettlementOfTerminalMoneyTests(_PaymentFixture):
    """Unidentified money leaves gateway clearing like any other online payment."""

    def test_the_platforms_settlement_run_pays_an_unmatched_payment_to_the_branch(self):
        tp = self.pay(self.lekki_terminal, 180_000, email="stranger@example.com", fee=2_000)
        self.yesterday(tp.collection)

        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.assertEqual((run.gross, run.fees, run.amount), (180_000, 2_000, 178_000))
        self.settle_run(run)

        self.assertEqual(self.balance(self.lekki_bank.gl_account), 178_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)
        self.assertEqual(self.suspense(self.books, self.lekki), 180_000)  # Still nobody's yet.
        tp.collection.refresh_from_db()
        self.assertFalse(tp.collection.awaits_settlement)
        self.assert_books_agree()

        terminal_payments.match(tp, self.ada)  # Identified after the bank has the money.
        self.assertEqual(self.suspense(self.books, self.lekki), 0)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)

    def test_a_matched_payment_settles_the_same_way(self):
        tp = self.pay(self.lekki_terminal, 150_000, email="ada.okeye@example.com", fee=1_500)
        self.yesterday(tp.collection)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.settle_run(run)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 148_500)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        self.assert_books_agree()

    def test_a_direct_tenants_terminal_money_is_not_held_and_settles_from_a_statement_line(self):
        from .settlement import settle_collections

        self.settle_directly()
        terminals.sync_tenant_terminals(self.tenant)
        self.assertTrue(VirtualTerminal.objects.get(pk=self.lekki_terminal.pk).split_code)

        tp = self.pay(self.lekki_terminal, 180_000, email="stranger@example.com", fee=2_000)

        self.assertFalse(tp.collection.held_by_platform)
        self.assertFalse(HeldMovement.objects.exists())
        self.assertEqual(tp.collection.metadata["settlement_subaccount"], "ACCT_LEK")
        self.assertTrue(tp.collection.awaits_settlement)
        settle_collections(self.line(self.lekki_bank, 178_000), [tp.collection.pk])
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 178_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        tp.collection.refresh_from_db()
        self.assertFalse(tp.collection.awaits_settlement)

        terminal_payments.match(tp, self.ada)
        self.assertEqual(self.suspense(self.books, self.lekki), 0)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)

    def test_a_terminal_not_yet_routed_in_a_direct_tenant_is_held_not_lost(self):
        self.settle_directly()  # The split has not been attached yet.
        tp = self.pay(self.lekki_terminal, 180_000, email="stranger@example.com", fee=2_000)
        self.assertTrue(tp.collection.held_by_platform)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)


class TerminalPaymentApiTests(_PaymentFixture):
    """Who may list, match and unmatch: every route is gated, tenant-bound and branch-bound."""

    VIEW = ("payments.terminal_payment.view",)
    ALL = ("payments.terminal_payment.view", "payments.terminal_payment.match",
           "payments.terminal_payment.reverse", "payments.terminal.view", "payments.terminal.update")

    def api(self, *keys, tenant=None, branch=None):
        n = next(_people)
        tenant = tenant or self.tenant
        user = self.grant(self.user_for(tenant, f"clerk{n}@{tenant.slug}.test"), *keys,
                          tenant=tenant, role_key=f"clerk-{n}", branch=branch)
        client = TenantAPIClient(user=user)
        client.role = TenantRoleTemplate.objects.get(tenant=tenant, key=f"clerk-{n}")
        return client

    def url(self, path, entity=None):
        joiner = "&" if "?" in path else "?"
        return f"/v1/payments/{path}{joiner}entity={(entity or self.books).code}"

    def get(self, client, path, entity=None):
        return client.get(self.url(path, entity))

    def post(self, client, path, body=None, entity=None):
        return client.post(self.url(path, entity), body or {}, format="json")

    def rows(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data["data"]

    # -- who is refused ---------------------------------------------------------- #

    def test_every_route_answers_403_to_a_caller_without_its_key(self):
        tp = self.pay(self.lekki_terminal, email="stranger@example.com")
        nobody = self.api("payments.report.view")
        for method, path in (
            ("get", "terminals/"), ("post", f"terminals/{self.lekki_terminal.pk}/retry/"),
            ("get", "terminal-payments/"), ("get", f"terminal-payments/{tp.pk}/"),
            ("post", f"terminal-payments/{tp.pk}/match/"),
            ("post", f"terminal-payments/{tp.pk}/unmatch/"),
        ):
            with self.subTest(path=path):
                response = getattr(self, method)(nobody, path)
                self.assertEqual(response.status_code, 403)

    def test_each_action_needs_its_own_key(self):
        tp = self.pay(self.lekki_terminal, email="stranger@example.com")
        viewer = self.api(*self.VIEW, "payments.terminal.view")
        self.assertEqual(self.post(viewer, f"terminal-payments/{tp.pk}/match/", {"customer": "CADA"}).status_code, 403)
        self.assertEqual(self.post(viewer, f"terminal-payments/{tp.pk}/unmatch/").status_code, 403)
        self.assertEqual(self.post(viewer, f"terminals/{self.lekki_terminal.pk}/retry/").status_code, 403)
        matcher = self.api(*self.VIEW, "payments.terminal_payment.match")
        self.assertEqual(self.post(matcher, f"terminal-payments/{tp.pk}/unmatch/").status_code, 403)

    def test_an_unauthenticated_caller_is_refused(self):
        response = TenantAPIClient().get(
            f"/v1/payments/terminal-payments/?entity={self.books.code}&tenant={self.tenant.slug}")
        self.assertIn(response.status_code, (401, 403))

    # -- tenant and branch isolation ---------------------------------------------- #

    def rival_payment(self):
        terminal = terminals.provision_terminal(terminals.ensure_terminal(self.rival_branch).pk)
        return self.pay(terminal, 77_000, email="rival@example.com")

    def test_another_tenants_terminals_and_payments_are_invisible_and_unreachable(self):
        rival = self.rival_payment()
        self.pay(self.lekki_terminal, email="stranger@example.com")
        admin = self.api(*self.ALL)

        listed = self.rows(self.get(admin, "terminal-payments/?status=ALL"))
        self.assertNotIn(rival.pk, [row["id"] for row in listed])
        self.assertEqual(self.get(admin, f"terminal-payments/{rival.pk}/").status_code, 404)
        self.assertEqual(self.post(admin, f"terminal-payments/{rival.pk}/match/", {"customer": "CADA"}).status_code, 404)
        self.assertEqual(self.post(admin, f"terminal-payments/{rival.pk}/unmatch/").status_code, 404)
        terminal_ids = {row["id"] for row in self.rows(self.get(admin, "terminals/"))}
        self.assertNotIn(rival.terminal_id, terminal_ids)
        self.assertEqual(self.post(admin, f"terminals/{rival.terminal_id}/retry/").status_code, 404)
        rival.refresh_from_db()
        self.assertEqual(rival.status, TerminalPaymentStatus.MATCHED)  # Untouched (matched by email).

        # The rival's own admin sees theirs and nothing of Corona's.
        theirs = self.api(*self.ALL, tenant=self.rival_tenant)
        mine = self.rows(self.get(theirs, "terminal-payments/?status=ALL", entity=self.rival_books))
        self.assertEqual([row["id"] for row in mine], [rival.pk])

    def test_a_branch_bound_clerk_sees_and_acts_on_their_own_branchs_payments_only(self):
        lekki = self.pay(self.lekki_terminal, 31_000, email="stranger@example.com")
        ikeja = self.pay(self.ikeja_terminal, 32_000, email="stranger@example.com")
        clerk = self.api(*self.ALL, branch=self.ikeja)

        self.assertEqual([row["id"] for row in self.rows(self.get(clerk, "terminal-payments/"))], [ikeja.pk])
        self.assertEqual(self.get(clerk, f"terminal-payments/{lekki.pk}/").status_code, 404)
        self.assertEqual(self.post(clerk, f"terminal-payments/{lekki.pk}/match/", {"customer": "CADA"}).status_code, 404)
        self.assertEqual(self.post(clerk, f"terminal-payments/{lekki.pk}/unmatch/").status_code, 404)
        self.assertEqual([row["branch"] for row in self.rows(self.get(clerk, "terminals/"))], [self.ikeja.pk])
        self.assertEqual(self.post(clerk, f"terminals/{self.lekki_terminal.pk}/retry/").status_code, 404)
        lekki.refresh_from_db()
        self.assertEqual(lekki.status, TerminalPaymentStatus.UNMATCHED)

    def test_a_whole_school_reader_sees_every_branch_and_can_narrow_by_branch(self):
        self.pay(self.lekki_terminal, 31_000, email="stranger@example.com")
        self.pay(self.ikeja_terminal, 32_000, email="stranger@example.com")
        admin = self.api(*self.ALL)
        self.assertEqual({row["branch"] for row in self.rows(self.get(admin, "terminal-payments/"))},
                         {self.lekki.pk, self.ikeja.pk})
        only = self.rows(self.get(admin, f"terminal-payments/?branch={self.lekki.pk}"))
        self.assertEqual({row["branch"] for row in only}, {self.lekki.pk})

    def test_a_one_branch_tenant_works_the_same(self):
        tp = self.pay(self.solo_terminal, 12_000, email="other@example.com")
        solo = self.api(*self.ALL, tenant=self.solo_tenant)
        rows = self.rows(self.get(solo, "terminal-payments/", entity=self.solo_books))
        self.assertEqual([row["id"] for row in rows], [tp.pk])
        response = self.post(solo, f"terminal-payments/{tp.pk}/match/", {"customer": "CSOLO"},
                             entity=self.solo_books)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "MATCHED")
        self.assertEqual(len(self.rows(self.get(solo, "terminals/", entity=self.solo_books))), 1)

    # -- the lists --------------------------------------------------------------- #

    def test_the_list_defaults_to_unmatched_payments_each_with_its_suggestions(self):
        waiting = self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE")
        done = self.pay(self.lekki_terminal, 20_000, email="ada.okeye@example.com")
        admin = self.api(*self.ALL)

        rows = self.rows(self.get(admin, "terminal-payments/"))
        self.assertEqual([row["id"] for row in rows], [waiting.pk])
        row = rows[0]
        self.assertEqual((row["status"], row["amount"], row["payer_email"], row["sender_name"],
                          row["branch_name"], row["terminal_name"]),
                         ("UNMATCHED", 100_000, "family@example.com", "ADA OKEYE",
                          "Lekki Branch", "Corona Group - Lekki Branch"))
        self.assertEqual([s["code"] for s in row["suggestions"]], ["CBOL", "CTUN", "CADA"])
        self.assertIn("pagination", self.get(admin, "terminal-payments/").data)

        matched = self.rows(self.get(admin, "terminal-payments/?status=MATCHED"))
        self.assertEqual([r["id"] for r in matched], [done.pk])
        self.assertEqual((matched[0]["matched_customer_code"], matched[0]["match_method"], matched[0]["suggestions"]),
                         ("CADA", "AUTO", []))
        everything = self.rows(self.get(admin, "terminal-payments/?status=ALL"))
        self.assertEqual({r["id"] for r in everything}, {waiting.pk, done.pk})
        self.assertEqual(self.get(admin, "terminal-payments/?status=bogus").status_code, 400)

    def test_a_page_of_matched_payments_costs_the_same_however_many_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        admin = self.api(*self.ALL)
        for _ in range(3):
            self.pay(self.lekki_terminal, 11_000, email="ada.okeye@example.com")
        with CaptureQueriesContext(connection) as three:
            self.rows(self.get(admin, "terminal-payments/?status=MATCHED"))
        for _ in range(4):
            self.pay(self.lekki_terminal, 11_000, email="ada.okeye@example.com")
        with CaptureQueriesContext(connection) as seven:
            self.assertEqual(len(self.rows(self.get(admin, "terminal-payments/?status=MATCHED"))), 7)
        self.assertEqual(len(seven), len(three))

    def test_terminals_list_shows_status_error_and_the_payment_details(self):
        self.fake.terminal_error = "destinations required"
        failed = terminals.provision_terminal(terminals.ensure_terminal(self.yaba).pk)
        admin = self.api(*self.ALL)
        by_branch = {row["branch"]: row for row in self.rows(self.get(admin, "terminals/"))}

        lekki = by_branch[self.lekki.pk]
        self.assertEqual((lekki["status"], lekki["name"], lekki["code"], lekki["routed_to_branch_bank"]),
                         ("ACTIVE", "Corona Group - Lekki Branch", self.lekki_terminal.code, False))
        self.assertIn("paymentMethods", lekki["payment_details"])
        self.assertNotIn("destinations", lekki["payment_details"])
        self.assertEqual((by_branch[self.yaba.pk]["status"], by_branch[self.yaba.pk]["last_error"]),
                         ("FAILED", failed.last_error))
        self.assertIn("destinations required", failed.last_error)
        only_failed = self.rows(self.get(admin, "terminals/?status=FAILED"))
        self.assertEqual([row["branch"] for row in only_failed], [self.yaba.pk])
        self.assertEqual(self.get(admin, "terminals/?status=bogus").status_code, 400)

    def test_retry_opens_a_failed_terminal_and_refuses_a_deactivated_one(self):
        from .models import VirtualTerminal

        self.fake.terminal_error = "down"
        yaba = terminals.ensure_terminal(self.yaba)
        terminals.provision_terminal(yaba.pk)
        self.assertEqual(VirtualTerminal.objects.get(pk=yaba.pk).status, "FAILED")
        self.fake.terminal_error = ""
        admin = self.api(*self.ALL)

        response = self.post(admin, f"terminals/{yaba.pk}/retry/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["data"]["status"], response.data["data"]["last_error"]), ("ACTIVE", ""))
        # An open terminal is simply asked about again.
        self.assertEqual(self.post(admin, f"terminals/{yaba.pk}/retry/").status_code, 200)
        terminals.deactivate_terminal(VirtualTerminal.objects.get(pk=yaba.pk))
        refused = self.post(admin, f"terminals/{yaba.pk}/retry/")
        self.assertEqual(refused.status_code, 409)

    # -- matching over the API -------------------------------------------------- #

    def test_matching_and_unmatching_over_the_api(self):
        tp = self.pay(self.lekki_terminal, 150_000, email="stranger@example.com")
        admin = self.api(*self.ALL)

        response = self.post(admin, f"terminal-payments/{tp.pk}/match/", {"customer": self.ada.pk})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual((data["status"], data["match_method"], data["matched_customer_code"]),
                         ("MATCHED", "MANUAL", "CADA"))
        self.assertEqual(self.amounts_paid(self.ada_first, self.ada_second), [100_000, 50_000])
        self.assertEqual(self.post(admin, f"terminal-payments/{tp.pk}/match/", {"customer": "CTUN"}).status_code, 409)

        response = self.post(admin, f"terminal-payments/{tp.pk}/unmatch/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "UNMATCHED")
        self.assertEqual(self.post(admin, f"terminal-payments/{tp.pk}/unmatch/").status_code, 409)
        self.assertEqual(self.suspense(self.books, self.lekki), 150_000)

    def test_a_customer_by_code_works_and_a_missing_one_is_a_400(self):
        tp = self.pay(self.lekki_terminal, 15_000, email="stranger@example.com")
        admin = self.api(*self.ALL)
        self.assertEqual(self.post(admin, f"terminal-payments/{tp.pk}/match/", {}).status_code, 400)
        self.assertEqual(self.post(admin, f"terminal-payments/{tp.pk}/match/", {"customer": "NOPE"}).status_code, 400)
        self.assertEqual(self.post(admin, f"terminal-payments/{tp.pk}/match/", {"customer": "cada"}).status_code, 200)

    def test_a_whole_school_bursar_naming_another_branchs_customer_is_told_why(self):
        tp = self.pay(self.lekki_terminal, 15_000, email="stranger@example.com")
        admin = self.api(*self.ALL)
        response = self.post(admin, f"terminal-payments/{tp.pk}/match/", {"customer": "COKAF"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("another branch", json.dumps(response.data))
        tp.refresh_from_db()
        self.assertEqual(tp.status, TerminalPaymentStatus.UNMATCHED)

    def test_a_branch_clerk_cannot_choose_another_branchs_family(self):
        tp = self.pay(self.ikeja_terminal, 15_000, email="stranger@example.com")
        clerk = self.api(*self.ALL, branch=self.ikeja)
        response = self.post(clerk, f"terminal-payments/{tp.pk}/match/", {"customer": "CADA"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("customer of its own branch", json.dumps(response.data))
        # The family every branch shares, and their own branch's, are open to them.
        self.assertEqual(self.post(clerk, f"terminal-payments/{tp.pk}/match/", {"customer": "CSHR"}).status_code, 200)

    # -- Field Access ----------------------------------------------------------- #

    def hide(self, client, resource="payments.terminal_payment"):
        keys = install_declared_fields(resource)
        set_field_access(client.role, *keys, read=False, write=False)

    def test_a_caller_who_may_read_none_of_the_fields_gets_none_of_them_anywhere(self):
        tp = self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE",
                      bank="Zenith Bank")
        done = self.pay(self.lekki_terminal, 20_000, email="ada.okeye@example.com", sender="ADA OKEYE")
        closed = self.api(*self.ALL, "payments.collection.view")
        self.hide(closed)

        for path in ("terminal-payments/?status=ALL", f"terminal-payments/{tp.pk}/",
                     f"terminal-payments/{done.pk}/"):
            with self.subTest(path=path):
                text = json.dumps(self.get(closed, path).data)
                for secret in ("family@example.com", "ada.okeye@example.com", "ADA OKEYE",
                               "Zenith Bank", "CADA", "Ada Okeye", "suggestions", "CBOL"):
                    self.assertNotIn(secret, text)
        shown = self.rows(self.get(closed, "terminal-payments/"))[0]
        for hidden in ("status", "amount", "payer_email", "suggestions", "matched_customer"):
            self.assertNotIn(hidden, shown)

        # The collections list is another door to the same people.
        text = json.dumps(self.get(closed, "collections/").data)
        for secret in ("family@example.com", "ada.okeye@example.com", "ADA OKEYE", "CADA", "Ada Okeye"):
            self.assertNotIn(secret, text)

    def test_a_caller_who_may_read_only_the_payer_sees_the_payer_and_nothing_else_personal(self):
        tp = self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE")
        client = self.api(*self.ALL)
        keys = install_declared_fields("payments.terminal_payment")
        set_field_access(client.role, *keys, read=False, write=False)
        set_field_access(client.role, "payments.terminal_payment.payer_email",
                         "payments.terminal_payment.amount", read=True, write=False)
        row = self.get(client, f"terminal-payments/{tp.pk}/").data["data"]
        self.assertEqual((row["payer_email"], row["amount"]), ("family@example.com", 100_000))
        for hidden in ("sender_name", "sender_bank", "suggestions", "matched_customer", "status"):
            self.assertNotIn(hidden, row)

    def test_suggestions_are_not_even_ranked_for_a_caller_who_cannot_read_them(self):
        self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE")
        client = self.api(*self.ALL)
        self.hide(client)
        with patch.object(terminal_payments, "suggestions") as ranked:
            self.get(client, "terminal-payments/")
        ranked.assert_not_called()

    def test_the_terminal_payment_details_are_hidden_from_a_caller_who_may_not_read_them(self):
        client = self.api(*self.ALL)
        self.hide(client, "payments.terminal")
        row = self.rows(self.get(client, "terminals/"))[0]
        for hidden in ("payment_details", "code", "name", "status", "last_error", "routed_to_branch_bank"):
            self.assertNotIn(hidden, row)

    def test_the_sensitive_fields_are_closed_by_default_and_the_rest_open(self):
        tp = self.pay(self.lekki_terminal, 100_000, email="family@example.com", sender="ADA OKEYE")
        install_declared_fields("payments.terminal_payment", "payments.terminal")
        client = self.api(*self.ALL)  # No switch of its own: the defaults apply.
        row = self.get(client, f"terminal-payments/{tp.pk}/").data["data"]
        for open_by_default in ("amount", "status", "reference", "terminal_name", "paid_at", "matched_by_name"):
            self.assertIn(open_by_default, row)
        for closed_by_default in ("payer_email", "sender_name", "matched_customer", "suggestions"):
            self.assertNotIn(closed_by_default, row)

    def test_the_registry_marks_exactly_the_personal_fields_sensitive_and_none_writable(self):
        declaration = get_declaration("payments", "terminal_payment")
        sensitive = {spec.name for spec in declaration.fields if spec.sensitive}
        self.assertEqual(sensitive, {"payer_email", "sender_name", "matched_customer", "suggestions"})
        for spec in declaration.fields:
            self.assertFalse(spec.writable, spec.name)
            if spec.sensitive:
                self.assertEqual(spec.read_by, ("finance_admin",), spec.name)
        terminal = get_declaration("payments", "terminal")
        self.assertEqual({spec.name for spec in terminal.fields if spec.sensitive}, {"payment_details"})
        self.assertFalse(any(spec.writable for spec in terminal.fields))

    def test_nothing_in_the_body_changes_a_terminal_payment_but_the_customer(self):
        tp = self.pay(self.lekki_terminal, 15_000, email="stranger@example.com")
        admin = self.api(*self.ALL)
        response = self.post(admin, f"terminal-payments/{tp.pk}/match/", {
            "customer": "CADA", "payer_email": "evil@example.com", "amount": 1, "branch": self.ikeja.pk})
        self.assertEqual(response.status_code, 200)
        tp.refresh_from_db()
        tp.collection.refresh_from_db()
        self.assertEqual((tp.payer_email, tp.collection.amount, tp.branch_id),
                         ("stranger@example.com", 15_000, self.lekki.pk))
