"""Phase 6 tests — vs_payments gateway (collections + payouts + webhooks).

Everything runs against the in-memory :class:`FakeProvider` (registered over the default
provider name), so the suite exercises the full provider → service → ledger flow without a
single network call. The invariants under test are the ones that matter for a payment
gateway: a confirmed collection books exactly one receipt, a confirmed payout books
exactly one vendor payment, a bad webhook signature is rejected, and a **retried webhook
never double-books** (idempotency).

Run from ``apps/``:
    ../cx/bin/python manage.py test vs_payments --settings=apps.settings.local
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from vs_finance.models import (
    Account,
    Customer,
    FiscalPeriod,
    FiscalYear,
    Invoice,
    InvoiceLine,
    LedgerEntity,
    Payment,
    TaxCode,
)
from vs_finance.receivables import customer_credit_balance, post_invoice
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.models import Vendor, VendorPayment

from rest_framework.exceptions import ValidationError

from . import reconciliation, services, webhooks
from .constants import CollectionStatus, PayoutBatchStatus, PayoutStatus, VirtualAccountStatus
from .exceptions import (
    DuplicateWebhookError,
    PaymentStateError,
    ProviderError,
    ProviderNotConfiguredError,
    WebhookSignatureError,
)
from .models import (
    CollectionIntent,
    PaymentEvent,
    PayoutBatch,
    PayoutInstruction,
    VirtualAccount,
)
from .providers import registry
from .providers.fake import FakeProvider


# Group tests for Payments Fixture Mixin.
class _PaymentsFixtureMixin:
    """A seeded ledger entity with a customer, a vendor, and the Fake provider wired in.

    The fiscal year is built for *today's* year so a receipt/payout dated today always
    lands in an OPEN period (the booking date is ``date.today()``).
    """

    # Prepare or verify the build test path.
    def build(self):
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,
        )
        seed_chart_of_accounts(entity)
        today = datetime.date.today()
        year = FiscalYear.objects.create(
            entity=entity, year=today.year,
            start_date=datetime.date(today.year, 1, 1),
            end_date=datetime.date(today.year, 12, 31),
        )
        for m in range(1, 13):
            start = datetime.date(today.year, m, 1)
            end = (datetime.date(today.year, m + 1, 1) if m < 12
                   else datetime.date(today.year + 1, 1, 1)) - datetime.timedelta(days=1)
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=year, period_no=m,
                name=f"{today.year}-{m:02d}", start_date=start, end_date=end,
            )
        customer = Customer.objects.create(
            entity=entity, code="CUST1", name="Acme Ltd",
            billing_email="ar@acme.test",
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )
        vendor = Vendor.objects.create(
            entity=entity, code="SUPP1", name="Supplier Ltd",
            payable_account=Account.objects.get(entity=entity, code="2100"),
            default_expense_account=Account.objects.get(entity=entity, code="5300"),
        )
        self.fake = FakeProvider(secret="test-secret")
        # Resolve the default provider name and the fake's own name to this instance.
        registry.register("PAYSTACK", self.fake)
        registry.register("FAKE", self.fake)
        self.addCleanup(registry.unregister)
        return entity, customer, vendor

    # Prepare or verify the make posted invoice test path.
    def make_posted_invoice(self, entity, customer, *, amount):
        inv = Invoice.objects.create(
            entity=entity, customer=customer,
            invoice_date=datetime.date.today(), due_date=datetime.date.today(),
        )
        InvoiceLine.objects.create(
            invoice=inv, revenue_account=Account.objects.get(entity=entity, code="4100"),
            quantity=1, unit_price=amount, tax_code=None, line_no=1,
        )
        post_invoice(inv)
        return inv


# Group tests for Provider Tests.
class ProviderTests(_PaymentsFixtureMixin, TestCase):
    # Verify fake signature roundtrip behavior.
    def test_fake_signature_roundtrip(self):
        self.build()
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference="R1", status="SUCCEEDED", amount=1000,
        )
        self.assertTrue(self.fake.verify_signature(raw_body=raw, headers=headers))
        # Tamper the body → signature no longer matches.
        self.assertFalse(self.fake.verify_signature(raw_body=raw + b"x", headers=headers))

    # Verify registry override resolves fake behavior.
    def test_registry_override_resolves_fake(self):
        self.build()
        self.assertIs(registry.get_provider("PAYSTACK"), self.fake)


# Group tests for Collection Tests.
class CollectionTests(_PaymentsFixtureMixin, TestCase):
    # Verify initiate creates processing intent with checkout behavior.
    def test_initiate_creates_processing_intent_with_checkout(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, narration="Fees",
        )
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)
        self.assertTrue(intent.checkout_url)
        self.assertTrue(intent.provider_reference)
        self.assertTrue(
            PaymentEvent.objects.filter(
                action="COLLECTION_INITIATED", reference=intent.reference, succeeded=True,
            ).exists()
        )

    # Verify confirm via verify books receipt and pays invoice behavior.
    def test_confirm_via_verify_books_receipt_and_pays_invoice(self):
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=50000)
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, invoice=inv,
        )
        # Provider now reports success; confirm with no explicit status polls verify().
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        intent = services.confirm_collection(intent)

        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertIsNotNone(intent.payment_id)
        payment = Payment.objects.get(pk=intent.payment_id)
        self.assertEqual(payment.amount, 50000)
        self.assertEqual(payment.status, "POSTED")
        inv.refresh_from_db()
        self.assertEqual(inv.amount_paid, 50000)

    # Verify failed collection books nothing behavior.
    def test_failed_collection_books_nothing(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=9999, customer=customer)
        intent = services.confirm_collection(intent, status=CollectionStatus.FAILED)
        self.assertEqual(intent.status, CollectionStatus.FAILED)
        self.assertIsNone(intent.payment_id)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    # Verify confirm is idempotent behavior.
    def test_confirm_is_idempotent(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=20000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        # A second confirm must not book a second receipt.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify settled amount overrides requested behavior.
    def test_settled_amount_overrides_requested(self):
        """A provider that settles less than requested books the settled amount and
        keeps the requested amount for audit."""
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=50000, customer=customer)
        intent = services.confirm_collection(
            intent, status=CollectionStatus.SUCCEEDED, amount=48000,
        )
        self.assertEqual(intent.amount, 48000)
        payment = Payment.objects.get(pk=intent.payment_id)
        self.assertEqual(payment.amount, 48000)
        self.assertEqual(intent.metadata["requested_amount"], 50000)

    # Verify standalone receipt parks credit not auto settling behavior.
    def test_standalone_receipt_parks_credit_not_auto_settling(self):
        """A collection with no invoice must NOT auto-settle the customer's open
        invoices; the cash parks as customer credit instead."""
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=30000)
        intent = services.initiate_collection(entity=entity, amount=20000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        inv.refresh_from_db()
        self.assertEqual(inv.amount_paid, 0)  # not auto-settled
        self.assertGreater(customer_credit_balance(customer), 0)  # cash held as credit

    # Verify customerless initiate is rejected behavior.
    def test_customerless_initiate_is_rejected(self):
        entity, _, _ = self.build()
        with self.assertRaises(ValidationError):
            services.initiate_collection(entity=entity, amount=1000)
        self.assertFalse(CollectionIntent.objects.filter(entity=entity).exists())

    def test_invoice_must_belong_to_selected_customer(self):
        entity, customer, _ = self.build()
        other = Customer.objects.create(
            entity=entity, code="CUST2", name="Other Customer",
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )
        invoice = self.make_posted_invoice(entity, other, amount=50000)
        with self.assertRaises(ValidationError):
            services.initiate_collection(
                entity=entity, amount=50000, customer=customer, invoice=invoice,
            )
        self.assertFalse(CollectionIntent.objects.filter(entity=entity).exists())

    def test_invoice_collection_cannot_exceed_outstanding_balance(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=50000)
        with self.assertRaises(ValidationError):
            services.initiate_collection(
                entity=entity, amount=50001, customer=customer, invoice=invoice,
            )
        self.assertFalse(CollectionIntent.objects.filter(entity=entity).exists())

    # Verify one active virtual account per customer provider behavior.
    def test_one_active_virtual_account_per_customer_provider(self):
        entity, customer, _ = self.build()
        services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        with self.assertRaises(ValidationError):
            services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        self.assertEqual(
            VirtualAccount.objects.filter(entity=entity, customer=customer).count(), 1)

    # Verify inactive virtual account deposit is held behavior.
    def test_inactive_virtual_account_deposit_is_held(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        services.set_virtual_account_status(va, status="INACTIVE")
        intent = services.initiate_collection(entity=entity, amount=25000, customer=customer)
        intent.virtual_account = va
        intent.save(update_fields=["virtual_account"])
        with self.assertRaises(PaymentStateError):
            services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())


# Group tests for Webhook Tests.
class WebhookTests(_PaymentsFixtureMixin, TestCase):
    # Support the signed workflow.
    def _signed(self, *, event, reference, status, amount=0):
        return self.fake.build_webhook(
            event=event, reference=reference, status=status, amount=amount,
        )

    # Verify bad signature is rejected and books nothing behavior.
    def test_bad_signature_is_rejected_and_books_nothing(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=30000, customer=customer)
        raw, _ = self._signed(event="charge.success", reference=intent.reference, status="SUCCEEDED")
        with self.assertRaises(WebhookSignatureError):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw,
                                    headers={"x-fake-signature": "deadbeef"})
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    # Verify webhook confirms collection behavior.
    def test_webhook_confirms_collection(self):
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=40000)
        intent = services.initiate_collection(
            entity=entity, amount=40000, customer=customer, invoice=inv,
        )
        # The webhook only triggers re-verification; the provider's API is the source
        # of truth for status, so make verify agree the collection succeeded.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=40000,
        )
        # Processing is deferred to an on_commit-enqueued task; fire the callback so the
        # eager worker re-verifies and books before we assert the terminal state.
        with self.captureOnCommitCallbacks(execute=True):
            event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        event.refresh_from_db()
        self.assertEqual(event.status, "PROCESSED")
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify webhook received event carries the matched record's entity behavior.
    def test_webhook_received_event_is_attributed_to_the_entity(self):
        # The WEBHOOK_RECEIVED audit row must carry the collection's entity so it shows
        # in that entity's transactions log (TransactionsLogView filters by entity).
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=40000,
        )
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        received = PaymentEvent.objects.filter(entity=entity, action="WEBHOOK_RECEIVED")
        self.assertTrue(received.exists())
        self.assertEqual(received.first().entity_id, intent.entity_id)

    # Verify duplicate webhook never double books behavior.
    def test_duplicate_webhook_never_double_books(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=25000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # provider verify agrees
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=25000,
        )
        # First delivery processes (fire the enqueued task); it flips the event to PROCESSED.
        with self.captureOnCommitCallbacks(execute=True):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        # The provider retries the exact same event — rejected before enqueue, so no wrapping.
        with self.assertRaises(DuplicateWebhookError):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify webhook does not book when provider verify disagrees behavior.
    def test_webhook_does_not_book_when_provider_verify_disagrees(self):
        # SECURITY: a validly-signed "charge.success" must NOT book a receipt if the
        # provider's own API doesn't confirm the transaction settled. The event is only
        # a trigger to re-verify — here verify returns PENDING (no forced_status), so
        # nothing is booked despite the webhook claiming SUCCEEDED.
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=30000, customer=customer)
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=30000,
        )
        # Fire the enqueued task so the re-verify actually runs; it returns PENDING (no
        # forced_status), so despite the signed "SUCCEEDED" claim nothing is booked.
        with self.captureOnCommitCallbacks(execute=True):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    # Verify re-ingesting the same event audits once and books once.
    def test_ingest_is_idempotent_and_audits_once(self):
        # A provider that retries a not-yet-processed event must not add a second
        # WEBHOOK_RECEIVED audit row (audit-once = `if created`) nor a second receipt.
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=27000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # provider verify agrees
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=27000,
        )
        # Ingest the same signed event twice; the second is a PROCESSED-duplicate short-circuit.
        with self.captureOnCommitCallbacks(execute=True):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        with self.assertRaises(DuplicateWebhookError):
            with self.captureOnCommitCallbacks(execute=True):
                webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        received = PaymentEvent.objects.filter(
            action="WEBHOOK_RECEIVED", reference=intent.reference,
        )
        self.assertEqual(received.count(), 1)  # exactly one audit row despite two deliveries
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)  # booked once


# Group tests for Payout Tests.
class PayoutTests(_PaymentsFixtureMixin, TestCase):
    # Verify initiate then confirm books vendor payment behavior.
    def test_initiate_then_confirm_books_vendor_payment(self):
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=70000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor, narration="Settle bill",
        )
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        payout = services.confirm_payout(payout, status=PayoutStatus.PAID)
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertIsNotNone(payout.vendor_payment_id)
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.gross_amount, 70000)
        self.assertEqual(vp.status, "POSTED")

    # Verify payout webhook confirms behavior.
    def test_payout_webhook_confirms(self):
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        # Webhook triggers re-verification; provider verify_transfer is the source of truth.
        self.fake.forced_status[payout.reference] = "PAID"
        raw, headers = self.fake.build_webhook(
            event="transfer.success", reference=payout.reference, status="PAID", amount=15000,
        )
        # Processing is deferred to an on_commit-enqueued task; fire it before asserting.
        with self.captureOnCommitCallbacks(execute=True):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(VendorPayment.objects.filter(entity=entity).count(), 1)

    # Verify payout adopts the provider-reported settled amount behavior.
    def test_payout_adopts_provider_settled_amount(self):
        # Mirror of confirm_collection: the verify path books what actually left the
        # account, retaining the instructed amount in metadata.
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=70000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        # Provider verify reports PAID for 68,000 (a 2,000 shortfall vs the 70,000 instructed).
        self.fake.forced_amount[payout.reference] = 68000
        self.fake.forced_status[payout.reference] = "PAID"
        payout = services.confirm_payout(payout)  # No explicit amount → verify path adopts result.amount.
        self.assertEqual(payout.amount, 68000)
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.gross_amount, 68000)
        self.assertEqual(payout.metadata["instructed_amount"], 70000)

    # Verify an explicit PAID status without an amount never overrides behavior.
    def test_confirm_payout_status_without_amount_keeps_instructed(self):
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=70000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        # status supplied, amount omitted → settled falls back to the instructed 70,000.
        payout = services.confirm_payout(payout, status=PayoutStatus.PAID)
        self.assertEqual(payout.amount, 70000)
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.gross_amount, 70000)
        self.assertNotIn("instructed_amount", payout.metadata or {})

    # Verify failed payout books nothing behavior.
    def test_failed_payout_books_nothing(self):
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        payout = services.confirm_payout(payout, status=PayoutStatus.FAILED)
        self.assertEqual(payout.status, PayoutStatus.FAILED)
        self.assertIsNone(payout.vendor_payment_id)
        self.assertFalse(VendorPayment.objects.filter(entity=entity).exists())


# Group tests for Flaky Provider.
class _FlakyProvider(FakeProvider):
    """A FakeProvider that refuses to transfer a sentinel amount (to fail one item)."""

    # Initialize this object with its required state.
    def __init__(self, *, fail_amount, **kwargs):
        super().__init__(**kwargs)
        self.fail_amount = fail_amount  # Fail the test when the invariant is violated.

    # Build or verify the create transfer test path.
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        if amount == self.fail_amount:  # Branch test setup or assertions.
            from .exceptions import ProviderError
            raise ProviderError("Beneficiary account rejected by the bank.")
        return super().create_transfer(
            reference=reference, amount=amount, currency=currency,
            account_number=account_number, bank_code=bank_code,
            account_name=account_name, narration=narration, metadata=metadata,
        )


# Group tests for Payout Batch Tests.
class PayoutBatchTests(_PaymentsFixtureMixin, TestCase):
    # Support the items workflow.
    def _items(self, vendor, *amounts):
        return [
            {"amount": amt, "beneficiary_name": "Supplier Ltd",
             "beneficiary_account_number": f"012345678{i}", "beneficiary_bank_code": "058",
             "vendor": vendor}
            for i, amt in enumerate(amounts)
        ]

    # Verify create batch assembles pending instructions without submitting behavior.
    def test_create_batch_assembles_pending_instructions_without_submitting(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(
            entity=entity, items=self._items(vendor, 10000, 20000, 30000),
            title="June payroll",
        )
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        self.assertEqual(batch.item_count, 3)
        self.assertEqual(batch.total_amount, 60000)
        self.assertEqual(batch.instructions.count(), 3)
        self.assertTrue(
            all(p.status == PayoutStatus.PENDING for p in batch.instructions.all())
        )
        # Nothing was sent to the provider yet → no provider_reference.
        self.assertTrue(all(not p.provider_reference for p in batch.instructions.all()))

    # Verify submit batch dispatches every item behavior.
    def test_submit_batch_dispatches_every_item(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))
        batch = services.submit_payout_batch(batch)
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)
        self.assertIsNotNone(batch.submitted_at)
        self.assertTrue(
            all(p.status == PayoutStatus.PROCESSING for p in batch.instructions.all())
        )

    # Verify confirming all items completes the batch behavior.
    def test_confirming_all_items_completes_the_batch(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))
        services.submit_payout_batch(batch)
        for payout in batch.instructions.all():
            services.confirm_payout(payout, status=PayoutStatus.PAID)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.COMPLETED)
        self.assertEqual(
            VendorPayment.objects.filter(entity=entity).count(), 2
        )

    # Verify partial failure marks batch partially completed behavior.
    def test_partial_failure_marks_batch_partially_completed(self):
        entity, _, vendor = self.build()
        flaky = _FlakyProvider(secret="test-secret", fail_amount=7000)
        registry.register("PAYSTACK", flaky)
        registry.register("FAKE", flaky)
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))
        services.submit_payout_batch(batch)
        # One dispatched (PROCESSING), one rejected at submit (FAILED).
        statuses = sorted(p.status for p in batch.instructions.all())
        self.assertEqual(statuses, [PayoutStatus.FAILED, PayoutStatus.PROCESSING])
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)
        # Confirm the surviving item → batch settles as PARTIALLY_COMPLETED.
        survivor = batch.instructions.get(status=PayoutStatus.PROCESSING)
        services.confirm_payout(survivor, status=PayoutStatus.PAID)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PARTIALLY_COMPLETED)


# Group tests for Settlement Reconciliation Tests.
class SettlementReconciliationTests(_PaymentsFixtureMixin, TestCase):
    # Support the bank account workflow.
    def _bank_account(self, entity):
        from vs_finance.models import BankAccount
        return BankAccount.objects.create(
            entity=entity, name="Operations",
            gl_account=Account.objects.get(entity=entity, code="1100"),
        )

    # Support the bank line workflow.
    def _bank_line(self, bank_account, *, amount, reference="", description="", day=None):
        from vs_finance.models import BankStatementLine
        return BankStatementLine.objects.create(
            bank_account=bank_account, txn_date=day or datetime.date.today(),
            description=description, reference=reference, amount=amount,
        )

    # Verify reference match settles a collection behavior.
    def test_reference_match_settles_a_collection(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        intent.refresh_from_db()
        ba = self._bank_account(entity)
        self._bank_line(ba, amount=40000, reference=intent.reference)

        recon = reconciliation.settlement_reconciliation(entity)
        self.assertEqual(recon.settled_count, 1)
        self.assertEqual(recon.unsettled_count, 0)
        self.assertEqual(recon.rows[0].match_basis, "reference")
        self.assertTrue(recon.is_reconciled)

    # Verify matched row carries net and fee from the bank line behavior.
    def test_matched_row_carries_net_and_fee_from_the_bank_line(self):
        """A collection booked at gross that settles to the bank net-of-fee exposes the
        PSP fee (gross − net) and the bank settlement reference on the matched row."""
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        intent.refresh_from_db()
        ba = self._bank_account(entity)
        # Bank received 39,100 net of a 900 PSP fee, under a settlement reference.
        self._bank_line(ba, amount=39100, reference=intent.reference, description="STL-PSK-0001")

        row = reconciliation.settlement_reconciliation(entity).rows[0]
        self.assertEqual(row.amount, 40000)         # gross (gateway)
        self.assertEqual(row.settled_amount, 39100)  # net (bank)
        self.assertEqual(row.fee_amount, 900)        # PSP fee
        self.assertEqual(row.settlement_reference, intent.reference)

    # Verify an over-settlement never yields a negative fee behavior.
    def test_over_settlement_fee_is_clamped_to_zero(self):
        # If the matched bank line is larger than the gateway amount, the derived fee
        # (gross − net) would go negative — it must clamp to 0 instead.
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        intent.refresh_from_db()
        ba = self._bank_account(entity)
        self._bank_line(ba, amount=40500, reference=intent.reference)  # Bank shows MORE than gateway.

        row = reconciliation.settlement_reconciliation(entity).rows[0]
        self.assertEqual(row.settled_amount, 40500)
        self.assertEqual(row.fee_amount, 0)

    # Verify amount fallback match for a payout behavior.
    def test_amount_fallback_match_for_a_payout(self):
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        services.confirm_payout(payout, status=PayoutStatus.PAID)
        ba = self._bank_account(entity)
        # No shared reference, but the signed amount matches (-15000 outflow).
        self._bank_line(ba, amount=-15000, reference="GTB-REF-XYZ")

        recon = reconciliation.settlement_reconciliation(entity)
        self.assertEqual(recon.settled_count, 1)
        self.assertEqual(recon.rows[0].amount, -15000)
        self.assertEqual(recon.rows[0].match_basis, "amount")
        self.assertTrue(recon.is_reconciled)

    # Verify the amount fallback pairs each row with the date-nearest bank line.
    def test_amount_match_prefers_the_date_nearest_bank_line(self):
        """Two same-amount collections and two same-amount, reference-less bank lines: the
        amount fallback must pair each row with the bank line whose date is nearest its
        confirmation, not by insertion order."""
        entity, customer, _ = self.build()
        intentA = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(intentA, status=CollectionStatus.SUCCEEDED)
        intentA.refresh_from_db()
        intentB = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(intentB, status=CollectionStatus.SUCCEEDED)
        intentB.refresh_from_db()
        # Push the two confirmations well apart so the nearest-date choice is unambiguous.
        intentA.confirmed_at = timezone.now() - datetime.timedelta(days=5)
        intentA.save(update_fields=["confirmed_at"])
        intentB.confirmed_at = timezone.now() - datetime.timedelta(days=1)
        intentB.save(update_fields=["confirmed_at"])

        ba = self._bank_account(entity)
        today = datetime.date.today()
        # No references → forces the amount fallback; one line lands near A, one near B.
        line_near_a = self._bank_line(ba, amount=40000, day=today - datetime.timedelta(days=5))
        line_near_b = self._bank_line(ba, amount=40000, day=today - datetime.timedelta(days=1))

        recon = reconciliation.settlement_reconciliation(entity)
        by_gateway = {r.gateway_id: r for r in recon.rows}
        row_a = by_gateway[intentA.id]
        row_b = by_gateway[intentB.id]
        # Each row takes the bank line nearest its own confirmation date, order-independent.
        self.assertEqual(row_a.matched_bank_line_id, line_near_a.id)
        self.assertEqual(row_b.matched_bank_line_id, line_near_b.id)
        self.assertEqual(row_a.match_basis, "amount")
        self.assertEqual(row_b.match_basis, "amount")
        self.assertTrue(recon.is_reconciled)

    # Verify amount-only matches are flagged for review; reference matches are not.
    def test_amount_only_matches_are_flagged_needs_review(self):
        entity, customer, vendor = self.build()
        # A reference-matched collection — trusted, not flagged.
        c = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(c, status=CollectionStatus.SUCCEEDED)
        c.refresh_from_db()
        # An amount-only-matched payout — ambiguous, flagged.
        p = services.initiate_payout(
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        services.confirm_payout(p, status=PayoutStatus.PAID)
        ba = self._bank_account(entity)
        self._bank_line(ba, amount=40000, reference=c.reference)     # reference match
        self._bank_line(ba, amount=-15000, reference="GTB-REF-XYZ")  # amount-only match

        recon = reconciliation.settlement_reconciliation(entity)
        by_basis = {r.match_basis: r for r in recon.rows}
        self.assertFalse(by_basis["reference"].needs_review)
        self.assertTrue(by_basis["amount"].needs_review)
        self.assertEqual(recon.needs_review_count, 1)

    # Verify unsettled gateway and unexplained bank line break reconciliation behavior.
    def test_unsettled_gateway_and_unexplained_bank_line_break_reconciliation(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=22000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        ba = self._bank_account(entity)
        # A bank line that matches nothing (wrong amount, no shared reference).
        self._bank_line(ba, amount=99999, reference="MYSTERY", description="Bank charge")

        recon = reconciliation.settlement_reconciliation(entity)
        self.assertEqual(recon.unsettled_count, 1)
        self.assertEqual(len(recon.unmatched_bank_lines), 1)
        self.assertFalse(recon.is_reconciled)

    # Verify date window filters both sides behavior.
    def test_date_window_filters_both_sides(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=12000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        ba = self._bank_account(entity)
        self._bank_line(ba, amount=12000, reference=intent.reference)
        # A window entirely in the past excludes today's confirmation and bank line.
        past = datetime.date.today() - datetime.timedelta(days=10)
        recon = reconciliation.settlement_reconciliation(
            entity, start_date=past - datetime.timedelta(days=5), end_date=past,
        )
        self.assertEqual(recon.rows, [])
        self.assertEqual(recon.unmatched_bank_lines, [])
        self.assertTrue(recon.is_reconciled)


# Group tests for Payment Event Tests.
class PaymentEventTests(_PaymentsFixtureMixin, TestCase):
    # Verify payment event is append only behavior.
    def test_payment_event_is_append_only(self):
        entity, customer, _ = self.build()
        services.initiate_collection(entity=entity, amount=10000, customer=customer)
        ev = PaymentEvent.objects.filter(action="COLLECTION_INITIATED").first()
        self.assertIsNotNone(ev)
        ev.message = "tampered"
        with self.assertRaises(ValueError):
            ev.save()
        with self.assertRaises(ValueError):
            ev.delete()


# Group tests for Payments A P I Tests.
class PaymentsAPITests(_PaymentsFixtureMixin, TestCase):
    """The /v1/payments/ REST surface, authenticated as a Vision super admin."""

    # Prepare or verify the setUp test path.
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant

        User = get_user_model()
        self.user = User.objects.create_user(
            email="pay-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Pay", last_name="Admin",
        )
        role, _ = TenantRoleTemplate.objects.get_or_create(tenant=Tenant.objects.get(slug="codex"), key="xvs_super_admin", defaults={"name": "Super Admin", "status": "ACTIVE"})
        TenantUserRoleAssignment.objects.create(tenant=Tenant.objects.get(slug="codex"), 
            user=self.user, role=role, assignment_status="ACTIVE",
        )
        from core.test_utils import TenantAPIClient
        self.client = TenantAPIClient(user=self.user)

    # Verify initiate collection endpoint behavior.
    def test_initiate_collection_endpoint(self):
        entity, customer, _ = self.build()
        resp = self.client.post(
            f"/v1/payments/collections/?entity={entity.code}",
            {"amount": 50000, "customer": customer.pk, "narration": "Fees"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual(data["status"], CollectionStatus.PROCESSING)
        self.assertTrue(data["checkout_url"])

    # Verify collection detail verify confirms behavior.
    def test_collection_detail_verify_confirms(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=12000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        resp = self.client.get(
            f"/v1/payments/collections/{intent.pk}/?entity={entity.code}&verify=1"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], CollectionStatus.SUCCEEDED)

    # Verify webhook endpoint processes and dedupes behavior.
    def test_webhook_endpoint_processes_and_dedupes(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=33000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # provider verify agrees
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=33000,
        )
        sig = headers["x-fake-signature"]
        # The HTTP response only acks (RECEIVED under a real worker); processing is the
        # on_commit-enqueued task, so fire the callback and assert the booked state via DB.
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                "/v1/payments/webhooks/PAYSTACK/", data=raw,
                content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)
        # A retry is acknowledged (200) but does not re-book (rejected before enqueue).
        second = self.client.post(
            "/v1/payments/webhooks/PAYSTACK/", data=raw,
            content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["data"].get("duplicate"))
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify initiate payout endpoint behavior.
    def test_initiate_payout_endpoint(self):
        entity, _, vendor = self.build()
        resp = self.client.post(
            f"/v1/payments/payouts/?entity={entity.code}",
            {"amount": 60000, "beneficiary_name": "Supplier Ltd",
             "beneficiary_account_number": "0123456789", "beneficiary_bank_code": "058",
             "vendor": vendor.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["status"], PayoutStatus.PROCESSING)

    # Verify create and submit payout batch endpoint behavior.
    def test_create_and_submit_payout_batch_endpoint(self):
        entity, _, vendor = self.build()
        resp = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"title": "Run", "submit": True,
             "items": [
                 {"amount": 11000, "beneficiary_name": "Supplier Ltd",
                  "beneficiary_account_number": "0123456789", "beneficiary_bank_code": "058",
                  "vendor": vendor.pk},
                 {"amount": 22000, "beneficiary_name": "Supplier Ltd",
                  "beneficiary_account_number": "0123456780", "beneficiary_bank_code": "058",
                  "vendor": vendor.pk},
             ]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual(data["status"], PayoutBatchStatus.PROCESSING)
        self.assertEqual(data["item_count"], 2)
        self.assertEqual(data["total_amount"], 33000)
        self.assertEqual(len(data["instructions"]), 2)

    # Verify batch endpoint resolves vendor by code and requires one behavior.
    def test_batch_endpoint_resolves_vendor_by_code_and_requires_one(self):
        entity, _, vendor = self.build()
        # vendor by CODE (the picker emits codes) + per-line WHT
        ok = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"title": "By code", "items": [
                {"amount": 40000, "wht_amount": 4000, "beneficiary_name": "Supplier Ltd",
                 "beneficiary_account_number": "0123456789", "vendor": vendor.code},
            ]},
            format="json",
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["data"]["instructions"][0]["wht_amount"], 4000)
        # a line with no vendor is rejected (it could never book)
        bad = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"items": [{"amount": 40000, "beneficiary_name": "X",
                        "beneficiary_account_number": "0123456789"}]},
            format="json",
        )
        self.assertEqual(bad.status_code, 400, bad.content)

    # Verify payout batch summary queued reflects only in-flight children behavior.
    def test_payout_batch_summary_queued_counts_only_in_flight_children(self):
        entity, _, vendor = self.build()
        flaky = _FlakyProvider(secret="test-secret", fail_amount=7000)  # Fails the 7,000 item at submit.
        registry.register("PAYSTACK", flaky)
        registry.register("FAKE", flaky)
        resp = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"title": "Run", "submit": True, "items": [
                {"amount": 11000, "beneficiary_name": "Supplier Ltd",
                 "beneficiary_account_number": "0123456789", "beneficiary_bank_code": "058",
                 "vendor": vendor.pk},
                {"amount": 7000, "beneficiary_name": "Supplier Ltd",
                 "beneficiary_account_number": "0123456780", "beneficiary_bank_code": "058",
                 "vendor": vendor.pk},
            ]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        summary = self.client.get(f"/v1/payments/payout-batches/summary/?entity={entity.code}")
        self.assertEqual(summary.status_code, 200)
        # Only the surviving in-flight child (11,000) is queued money; the FAILED 7,000 is not.
        self.assertEqual(summary.json()["data"]["queued"]["kobo"], 11000)

    # Verify movements feed does not expose internal ledger ids behavior.
    def test_movements_feed_hides_internal_linked_id(self):
        entity, customer, vendor = self.build()
        services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.initiate_payout(
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        resp = self.client.get(f"/v1/payments/movements/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertTrue(rows)  # Both a collection and a payout row are present.
        for row in rows:  # Neither side should leak the internal payment/vendor-payment id.
            self.assertNotIn("linked_id", row)

    # Verify settlement reconciliation endpoint behavior.
    def test_settlement_reconciliation_endpoint(self):
        from vs_finance.models import BankAccount, BankStatementLine

        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        intent.refresh_from_db()
        ba = BankAccount.objects.create(
            entity=entity, name="Operations",
            gl_account=Account.objects.get(entity=entity, code="1100"),
        )
        BankStatementLine.objects.create(
            bank_account=ba, txn_date=datetime.date.today(),
            reference=intent.reference, amount=40000,
        )
        resp = self.client.get(
            f"/v1/payments/reports/settlement-reconciliation/?entity={entity.code}"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["is_reconciled"])
        self.assertEqual(data["summary"]["settled_count"], 1)

    # Verify transactions log endpoint behavior.
    def test_transactions_log_endpoint(self):
        entity, customer, _ = self.build()
        # Initiating a collection writes a COLLECTION_INITIATED PaymentEvent.
        services.initiate_collection(entity=entity, amount=40000, customer=customer)
        resp = self.client.get(f"/v1/payments/transactions/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertTrue(any(r["action"] == "COLLECTION_INITIATED" for r in rows))
        self.assertIn("action_display", rows[0])
        # The ?action= filter narrows the log.
        filtered = self.client.get(
            f"/v1/payments/transactions/?entity={entity.code}&action=PAYOUT_FAILED"
        )
        self.assertEqual(filtered.status_code, 200)
        # success_response coerces an empty list to {}, so assert it's falsy.
        self.assertFalse(filtered.json()["data"])

    # Verify virtual account provision list and status behavior.
    def test_virtual_account_provision_list_and_status(self):
        entity, customer, _ = self.build()

        # Provision via the Fake provider → mints a test NUBAN.
        created = self.client.post(
            f"/v1/payments/virtual-accounts/?entity={entity.code}",
            {"customer": customer.pk, "provider": "FAKE"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        va = created.json()["data"]
        self.assertEqual(va["status"], "ACTIVE")
        self.assertEqual(va["customer_code"], "CUST1")
        # super-admin holds view_sensitive → the funding number is visible.
        self.assertTrue(va["account_number"])

        # GET list is paginated and rides KPIs.
        listed = self.client.get(f"/v1/payments/virtual-accounts/?entity={entity.code}")
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertEqual(body["kpis"], {"total": 1, "active": 1, "inactive": 0, "providers": 1})
        self.assertEqual(body["pagination"]["totalItems"], 1)
        self.assertEqual(len(body["data"]), 1)

        # Status filter excludes the active one.
        inactive_list = self.client.get(
            f"/v1/payments/virtual-accounts/?entity={entity.code}&status=INACTIVE")
        self.assertFalse(inactive_list.json()["data"])

        # PATCH deactivates it.
        patched = self.client.patch(
            f"/v1/payments/virtual-accounts/{va['id']}/?entity={entity.code}",
            {"status": "INACTIVE"}, format="json")
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(patched.json()["data"]["status"], "INACTIVE")
        # KPIs reflect it.
        kpis = self.client.get(
            f"/v1/payments/virtual-accounts/?entity={entity.code}").json()["kpis"]
        self.assertEqual(kpis, {"total": 1, "active": 0, "inactive": 1, "providers": 1})

        # A bogus status is rejected.
        bad = self.client.patch(
            f"/v1/payments/virtual-accounts/{va['id']}/?entity={entity.code}",
            {"status": "SUSPENDED"}, format="json")
        self.assertEqual(bad.status_code, 400, bad.content)

    # Verify virtual account list uses standard envelope behavior.
    def test_virtual_account_list_uses_standard_envelope(self):
        entity, customer, _ = self.build()
        services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        resp = self.client.get(f"/v1/payments/virtual-accounts/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["pagination"]["pageSize"], 25)
        self.assertIn("kpis", body)
        self.assertEqual(len(body["data"]), 1)

    # Verify collections filter by virtual account behavior.
    def test_collections_filter_by_virtual_account(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        # A collection that arrived through that VA.
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        intent.virtual_account = va
        intent.save(update_fields=["virtual_account"])
        # Another, not linked to the VA.
        services.initiate_collection(entity=entity, amount=10000, customer=customer)

        scoped = self.client.get(
            f"/v1/payments/collections/?entity={entity.code}&virtual_account={va.id}")
        self.assertEqual(scoped.status_code, 200)
        rows = scoped.json()["data"]
        self.assertEqual([r["id"] for r in rows], [intent.id])


# Group tests for Payout Batch Approval (maker-checker over the cash-out path).
class PayoutBatchApprovalTests(TestCase):
    """Approval-gating bulk payout batches via vs_workflow (opt-in by template).

    A payout batch is the highest-risk cash-out path, so — when a
    ``payments.payout_batch`` template exists for its scope — provider submission
    happens only after approval; with no template, direct submit is unchanged.
    """

    APPROVE_KEY = "payments.payout_batch.approve"

    def setUp(self):
        import io
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from rest_framework.test import APIClient
        from vs_rbac.models import (
            TenantRoleTemplate, TenantUserRoleAssignment, TenantRolePermission,
        )
        from vs_tenants.models import Tenant
        from vs_schools.models import School

        call_command("seed_payments_permissions", verbosity=0, stdout=io.StringIO())

        self.User = get_user_model()
        self.TenantRoleTemplate = TenantRoleTemplate
        self.TenantRolePermission = TenantRolePermission
        self.TenantUserRoleAssignment = TenantUserRoleAssignment

        # A school-owned entity so batch.school resolves and SCHOOL-scoped approver
        # resolution has a pool to draw from.
        self.school = School.objects.create(name="Cedar", slug="cedar-pba", code="CDRPBA", status="ACTIVE")
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Cedar Books", code="CDRBK", kind=LedgerEntity.Kind.TENANT,
            tenant=self.school.tenant,
        )
        seed_chart_of_accounts(self.entity)
        today = datetime.date.today()
        year = FiscalYear.objects.create(
            entity=self.entity, year=today.year,
            start_date=datetime.date(today.year, 1, 1),
            end_date=datetime.date(today.year, 12, 31),
        )
        for m in range(1, 13):
            start = datetime.date(today.year, m, 1)
            end = (datetime.date(today.year, m + 1, 1) if m < 12
                   else datetime.date(today.year + 1, 1, 1)) - datetime.timedelta(days=1)
            FiscalPeriod.objects.create(
                entity=self.entity, fiscal_year=year, period_no=m,
                name=f"{today.year}-{m:02d}", start_date=start, end_date=end,
            )
        self.vendor = Vendor.objects.create(
            entity=self.entity, code="SUPP1", name="Supplier Ltd",
            payable_account=Account.objects.get(entity=self.entity, code="2100"),
            default_expense_account=Account.objects.get(entity=self.entity, code="5300"),
        )
        self.fake = FakeProvider(secret="test-secret")
        registry.register("PAYSTACK", self.fake)
        registry.register("FAKE", self.fake)
        self.addCleanup(registry.unregister)

        # Requester: a school user holding every payments/finance key at this
        # school (the entity is school-owned, so only its tenant may address it;
        # approve verbs excluded so SoD scenarios stay meaningful).
        from vs_schools.models import Branch
        from vs_rbac.models import Permission, TenantRolePermission
        branch = Branch.objects.create(school=self.school, name="Main", is_main=True, status="ACTIVE")
        self.requester = self.User.objects.create_user(
            email="req-pba@test.com", password="pw", user_type="STAFF", status="ACTIVE",
            first_name="Req", last_name="Ester", branch=branch,
        )
        ops_role, created = TenantRoleTemplate.objects.get_or_create(
            tenant=self.school.tenant, key="payments-ops-all",
            defaults={"name": "Payments Ops (all keys)", "status": "ACTIVE"},
        )
        if created:
            keys = Permission.objects.filter(
                key__startswith="payments.",
            ) | Permission.objects.filter(key__startswith="finance.")
            TenantRolePermission.objects.bulk_create(
                [TenantRolePermission(role=ops_role, permission=p)
                 for p in keys.exclude(key__endswith=".approve")],
                ignore_conflicts=True,
            )
        TenantUserRoleAssignment.objects.create(
            tenant=self.school.tenant, user=self.requester, role=ops_role,
            assignment_status="ACTIVE",
        )
        from core.test_utils import TenantAPIClient
        self.client = TenantAPIClient(user=self.requester)

    # --- helpers ----------------------------------------------------------- #

    def _draft_batch(self, *amounts):
        items = [
            {"amount": a, "beneficiary_name": "Supplier Ltd",
             "beneficiary_account_number": f"012345678{i}", "beneficiary_bank_code": "058",
             "vendor": self.vendor}
            for i, a in enumerate(amounts or (10000,))
        ]
        return services.create_payout_batch(entity=self.entity, items=items, title="Run")

    def _publish_template(self, *, on_rejection="RETURN_TO_REQUESTER"):
        from vs_workflow.services.templates import publish_template

        return publish_template(
            tenant=self.school.tenant, branch=None,
            document_type="payments.payout_batch", code="standard",
            name="Payout batch approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_permission_key": self.APPROVE_KEY,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": on_rejection, "skip_if_no_approvers": False,
            }])

    def _make_approver(self, email="apr-pba@test.com"):
        user = self.User.objects.create_user(
            email=email, password="pw", user_type="SCHOOL_ADMIN", status="ACTIVE",
            first_name="Apr", last_name="Over", tenant=self.school.tenant,
        )
        role, _ = self.TenantRoleTemplate.objects.get_or_create(
            tenant=self.school.tenant, key="pba-checker",
            defaults={"name": "Payout Checker", "status": "ACTIVE"},
        )
        self.TenantRolePermission.objects.get_or_create(
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},
        )
        self.TenantUserRoleAssignment.objects.create(
            tenant=self.school.tenant, user=user, role=role, assignment_status="ACTIVE",
        )
        return user

    def _submit_for_approval(self, batch):
        return self.client.post(
            f"/v1/payments/payout-batches/{batch.pk}/submit-for-approval/?entity={self.entity.code}",
            {}, format="json")

    def _direct_submit(self, batch):
        return self.client.post(
            f"/v1/payments/payout-batches/{batch.pk}/?entity={self.entity.code}", {}, format="json")

    def _instance_for(self, batch):
        from vs_workflow.models import WorkflowInstance
        return WorkflowInstance.objects.for_document(batch).first()

    # --- 1. gate off: no template → direct submit works -------------------- #

    def test_gate_off_direct_submit_works(self):
        from vs_finance.approvals import approval_required

        batch = self._draft_batch(10000, 20000)
        self.assertFalse(approval_required(batch))
        resp = self._direct_submit(batch)
        self.assertEqual(resp.status_code, 200, resp.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)

    # --- 2. gate on: direct submit refused --------------------------------- #

    def test_gate_on_direct_submit_refused(self):
        self._publish_template()
        batch = self._draft_batch(10000)
        resp = self._direct_submit(batch)
        self.assertEqual(resp.status_code, 400, resp.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        self.assertTrue(all(p.status == PayoutStatus.PENDING for p in batch.instructions.all()))

    # --- 3. submit-for-approval: pending, no provider dispatch ------------- #

    def test_submit_for_approval_marks_pending_and_does_not_dispatch(self):
        self._publish_template()
        self._make_approver()  # keep the stage ACTIVE
        batch = self._draft_batch(10000)
        resp = self._submit_for_approval(batch)
        self.assertEqual(resp.status_code, 200, resp.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        self.assertEqual((batch.metadata or {}).get("approval_status"), "PENDING_APPROVAL")
        self.assertTrue(all(p.status == PayoutStatus.PENDING for p in batch.instructions.all()))

    # --- 4. SoD: requester cannot approve own batch ------------------------ #

    def test_requester_cannot_approve_own_batch(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.exceptions import (
            NotAnEligibleApproverError, RequesterCannotApproveError,
        )

        self._publish_template()
        self._make_approver()
        batch = self._draft_batch(10000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        with self.assertRaises((RequesterCannotApproveError, NotAnEligibleApproverError)):
            wf_actions.record_action(instance.id, self.requester, ActionEnum.APPROVED)
        batch.refresh_from_db()
        self.assertTrue(all(p.status == PayoutStatus.PENDING for p in batch.instructions.all()))

    # --- 5. happy path: approval dispatches the batch ---------------------- #

    def test_approval_dispatches_batch(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_template()
        approver = self._make_approver()
        batch = self._draft_batch(10000, 20000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)
        self.assertEqual((batch.metadata or {}).get("approval_status"), "APPROVED")
        self.assertTrue(all(p.status == PayoutStatus.PROCESSING for p in batch.instructions.all()))

    # --- 6. reject → back to draft, nothing dispatched --------------------- #

    def test_reject_returns_batch_to_draft(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._publish_template(on_rejection="TERMINAL")
        approver = self._make_approver()
        batch = self._draft_batch(10000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        wf_actions.record_action(instance.id, approver, ActionEnum.REJECTED, comment="no")

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        self.assertEqual((batch.metadata or {}).get("approval_status"), "DRAFT")
        self.assertTrue(all(p.status == PayoutStatus.PENDING for p in batch.instructions.all()))


# Group tests for Paystack Adapter Tests (real adapter, network function mocked).
class PaystackAdapterTests(TestCase):
    """Drive the real :class:`PaystackProvider` with recorded PSP payloads.

    Every network hop goes through ``vs_payments.providers.http.request_json``, imported
    into the paystack module as ``request_json`` — so we patch it *there* (at the point of
    use) and never open a socket. This exercises the adapter's real request/response
    mapping, signing, and webhook parsing rather than the FakeProvider.
    """

    # NOTE: paystack.py does `from .http import request_json`, so the name to patch is the
    # one bound in the paystack module, not http's (patching http would miss this call).
    PATCH_TARGET = "vs_payments.providers.paystack.request_json"

    # Prepare or verify the setUp test path.
    def setUp(self):
        from .providers.paystack import PaystackProvider
        self.provider = PaystackProvider(secret_key="sk_test_x")  # Real adapter, test secret.

    # Verify create checkout maps the hosted-checkout response behavior.
    def test_create_checkout_maps_response(self):
        resp = {"status": True, "data": {
            "reference": "R1", "authorization_url": "https://pay/x", "access_code": "AC"}}
        with patch(self.PATCH_TARGET, return_value=resp):
            result = self.provider.create_checkout(
                reference="R1", amount=40000, currency="NGN", customer_email="c@x.test")
        self.assertEqual(result.checkout_url, "https://pay/x")
        self.assertEqual(result.provider_reference, "R1")
        self.assertEqual(result.authorization_code, "AC")
        self.assertEqual(result.status, "PENDING")

    # Verify verify_collection maps the neutral status + kobo amount behavior.
    def test_verify_collection_maps_status_and_amount(self):
        resp = {"status": True, "data": {
            "status": "success", "id": 99, "amount": 40000, "currency": "NGN"}}
        with patch(self.PATCH_TARGET, return_value=resp):
            result = self.provider.verify_collection(reference="R1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.amount, 40000)
        self.assertEqual(result.provider_reference, "99")

    # Verify create_transfer (recipient + transfer) then verify_transfer behavior.
    def test_create_and_verify_transfer(self):
        # create_transfer makes TWO calls: /transferrecipient then /transfer.
        recipient = {"status": True, "data": {"recipient_code": "RCP"}}
        transfer = {"status": True, "data": {
            "status": "success", "transfer_code": "TRF", "amount": 15000}}
        with patch(self.PATCH_TARGET, side_effect=[recipient, transfer]):
            created = self.provider.create_transfer(
                reference="P1", amount=15000, currency="NGN",
                account_number="0123456789", bank_code="058", account_name="Payee")
        self.assertEqual(created.status, "PAID")
        self.assertEqual(created.recipient_code, "RCP")
        self.assertEqual(created.provider_reference, "TRF")
        # verify_transfer re-queries a single endpoint and reports the settled kobo amount.
        with patch(self.PATCH_TARGET, return_value=transfer):
            verified = self.provider.verify_transfer(reference="P1")
        self.assertEqual(verified.status, "PAID")
        self.assertEqual(verified.amount, 15000)

    # Verify a non-ok provider envelope raises ProviderError behavior.
    def test_non_ok_response_raises(self):
        with patch(self.PATCH_TARGET, return_value={"status": False, "message": "bad"}):
            with self.assertRaises(ProviderError):
                self.provider.verify_collection(reference="R1")

    # Verify HMAC-SHA512 signature verification behavior.
    def test_verify_signature_roundtrip(self):
        raw = b'{"event":"charge.success","data":{"reference":"R1"}}'
        sig = hmac.new(b"sk_test_x", raw, hashlib.sha512).hexdigest()  # The real Paystack scheme.
        self.assertTrue(self.provider.verify_signature(
            raw_body=raw, headers={"x-paystack-signature": sig}))
        # Tamper the body → the stored signature no longer matches.
        self.assertFalse(self.provider.verify_signature(
            raw_body=raw + b"x", headers={"x-paystack-signature": sig}))

    # Verify webhook parsing routes collection vs payout events behavior.
    def test_parse_webhook_routes_by_event(self):
        charge = {"event": "charge.success", "data": {
            "reference": "R1", "status": "success", "amount": 40000, "id": 99, "currency": "NGN"}}
        parsed = self.provider.parse_webhook(payload=charge, raw_body=b"", headers={})
        self.assertEqual(parsed.direction, "COLLECTION")
        self.assertEqual(parsed.status, "SUCCEEDED")
        transfer = {"event": "transfer.success", "data": {
            "reference": "P1", "status": "success", "transfer_code": "TRF", "amount": 15000}}
        parsed = self.provider.parse_webhook(payload=transfer, raw_body=b"", headers={})
        self.assertEqual(parsed.direction, "PAYOUT")
        self.assertEqual(parsed.status, "PAID")


# Group tests for OPay Adapter Tests (real adapter, network function mocked).
class OPayAdapterTests(TestCase):
    """Drive the real :class:`OPayProvider` with recorded PSP payloads.

    OPay wraps every result as ``{"code": "00000", "data": {...}}`` and models amounts as a
    nested ``{"total": <kobo>, ...}`` object; status queries authenticate with the public
    key. As with Paystack we patch the ``request_json`` bound in the opay module.
    """

    PATCH_TARGET = "vs_payments.providers.opay.request_json"  # Patch at the point of use.

    # Prepare or verify the setUp test path.
    def setUp(self):
        from .providers.opay import OPayProvider
        self.provider = OPayProvider(
            merchant_id="M", secret_key="sk", public_key="pk",
            create_path="/c", status_path="/s", transfer_path="/t", transfer_status_path="/ts")

    # Verify create_checkout maps the cashier response behavior.
    def test_create_checkout_maps_response(self):
        resp = {"code": "00000", "data": {"orderNo": "O1", "cashierUrl": "https://opay/x"}}
        with patch(self.PATCH_TARGET, return_value=resp):
            result = self.provider.create_checkout(
                reference="O1", amount=40000, currency="NGN")
        self.assertEqual(result.checkout_url, "https://opay/x")
        self.assertEqual(result.provider_reference, "O1")

    # Verify verify_collection unwraps the nested kobo amount behavior.
    def test_verify_collection_maps_status_and_amount(self):
        resp = {"code": "00000", "data": {
            "status": "SUCCESS", "orderNo": "O1", "amount": {"total": 40000, "currency": "NGN"}}}
        with patch(self.PATCH_TARGET, return_value=resp):
            result = self.provider.verify_collection(reference="O1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.amount, 40000)

    # Verify verify_transfer unwraps the nested kobo amount behavior.
    def test_verify_transfer_maps_status_and_amount(self):
        resp = {"code": "00000", "data": {
            "status": "SUCCESS", "orderNo": "O1", "amount": {"total": 15000}}}
        with patch(self.PATCH_TARGET, return_value=resp):
            result = self.provider.verify_transfer(reference="P1")
        self.assertEqual(result.status, "PAID")
        self.assertEqual(result.amount, 15000)

    # Verify a non-success code raises ProviderError behavior.
    def test_non_success_code_raises(self):
        with patch(self.PATCH_TARGET, return_value={"code": "E123", "message": "nope"}):
            with self.assertRaises(ProviderError):
                self.provider.verify_collection(reference="O1")

    # Verify virtual-account provisioning is unsupported behavior.
    def test_create_virtual_account_unsupported(self):
        # Raises before any network call, so no patch is needed.
        with self.assertRaises(ProviderError):
            self.provider.create_virtual_account(reference="O1", customer_name="Acme")

    # Verify the wrapped-body signature scheme behavior.
    def test_verify_signature_roundtrip(self):
        inner = {"reference": "O1", "status": "SUCCESS", "orderNo": "O1"}
        body = {"payload": inner, "sha512": self.provider.sign(inner)}  # OPay signs the inner object.
        raw = json.dumps(body).encode()
        self.assertTrue(self.provider.verify_signature(raw_body=raw, headers={}))
        # A wrong embedded signature fails.
        bad = json.dumps({"payload": inner, "sha512": "deadbeef"}).encode()
        self.assertFalse(self.provider.verify_signature(raw_body=bad, headers={}))

    # Verify webhook parsing routes transfer vs collection events behavior.
    def test_parse_webhook_routes_by_shape(self):
        payout = {"payload": {
            "reference": "P1", "status": "SUCCESS", "transferStatus": "SUCCESS",
            "orderNo": "O2", "amount": {"total": 15000}}}
        parsed = self.provider.parse_webhook(payload=payout, raw_body=b"", headers={})
        self.assertEqual(parsed.direction, "PAYOUT")
        collection = {"payload": {
            "reference": "O1", "status": "SUCCESS", "orderNo": "O1", "amount": {"total": 40000}}}
        parsed = self.provider.parse_webhook(payload=collection, raw_body=b"", headers={})
        self.assertEqual(parsed.direction, "COLLECTION")


# Group tests for Webhook Provider Resolution.
class WebhookProviderResolutionTests(TestCase):
    """Guard the webhook receiver's provider lookup."""

    # Verify an unknown provider is rejected before any processing behavior.
    def test_unknown_provider_raises_not_configured(self):
        with self.assertRaises(ProviderNotConfiguredError):
            webhooks.ingest_webhook(provider="nope", raw_body=b"{}", headers={})
