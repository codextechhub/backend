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
from __future__ import annotations  # Import project symbols exercised by these tests.

import datetime  # Import dependency used by this test module.

from django.test import TestCase  # Import project symbols exercised by these tests.

from vs_finance.models import (  # Import project symbols exercised by these tests.
    Account,  # Continue structured test data.
    Customer,  # Continue structured test data.
    FiscalPeriod,  # Continue structured test data.
    FiscalYear,  # Continue structured test data.
    Invoice,  # Continue structured test data.
    InvoiceLine,  # Continue structured test data.
    LedgerEntity,  # Continue structured test data.
    Payment,  # Continue structured test data.
    TaxCode,  # Continue structured test data.
)  # Close the grouped test expression.
from vs_finance.receivables import customer_credit_balance, post_invoice  # Import project symbols exercised by these tests.
from vs_finance.seed import seed_chart_of_accounts, seed_currencies  # Import project symbols exercised by these tests.
from vs_procurement.models import Vendor, VendorPayment  # Import project symbols exercised by these tests.

from rest_framework.exceptions import ValidationError  # Import project symbols exercised by these tests.

from . import reconciliation, services, webhooks  # Import project symbols exercised by these tests.
from .constants import CollectionStatus, PayoutBatchStatus, PayoutStatus, VirtualAccountStatus  # Import project symbols exercised by these tests.
from .exceptions import DuplicateWebhookError, PaymentStateError, WebhookSignatureError  # Import project symbols exercised by these tests.
from .models import (  # Import project symbols exercised by these tests.
    CollectionIntent,  # Continue structured test data.
    PaymentEvent,  # Continue structured test data.
    PayoutBatch,  # Continue structured test data.
    PayoutInstruction,  # Continue structured test data.
    VirtualAccount,  # Continue structured test data.
)  # Close the grouped test expression.
from .providers import registry  # Import project symbols exercised by these tests.
from .providers.fake import FakeProvider  # Import project symbols exercised by these tests.


class _PaymentsFixtureMixin:  # Define a test fixture or test case class.
    """A seeded ledger entity with a customer, a vendor, and the Fake provider wired in.

    The fiscal year is built for *today's* year so a receipt/payout dated today always
    lands in an OPEN period (the booking date is ``date.today()``).
    """

    def build(self):  # Define a test helper or test method.
        seed_currencies()  # Execute the test step.
        entity = LedgerEntity.objects.create(  # Create test database data.
            name="Test Books", code="TBOOK", kind=LedgerEntity.Kind.TENANT,  # Continue structured test data.
        )  # Close the grouped test expression.
        seed_chart_of_accounts(entity)  # Execute the test step.
        today = datetime.date.today()  # Assign test setup data.
        year = FiscalYear.objects.create(  # Create test database data.
            entity=entity, year=today.year,  # Continue structured test data.
            start_date=datetime.date(today.year, 1, 1),  # Continue structured test data.
            end_date=datetime.date(today.year, 12, 31),  # Continue structured test data.
        )  # Close the grouped test expression.
        for m in range(1, 13):  # Iterate through test data.
            start = datetime.date(today.year, m, 1)  # Assign test setup data.
            end = (datetime.date(today.year, m + 1, 1) if m < 12  # Assign test setup data.
                   else datetime.date(today.year + 1, 1, 1)) - datetime.timedelta(days=1)  # Assign test setup data.
            FiscalPeriod.objects.create(  # Create test database data.
                entity=entity, fiscal_year=year, period_no=m,  # Continue structured test data.
                name=f"{today.year}-{m:02d}", start_date=start, end_date=end,  # Continue structured test data.
            )  # Close the grouped test expression.
        customer = Customer.objects.create(  # Create test database data.
            entity=entity, code="CUST1", name="Acme Ltd",  # Continue structured test data.
            billing_email="ar@acme.test",  # Continue structured test data.
            receivable_account=Account.objects.get(entity=entity, code="1200"),  # Fetch test database data.
        )  # Close the grouped test expression.
        vendor = Vendor.objects.create(  # Create test database data.
            entity=entity, code="SUPP1", name="Supplier Ltd",  # Continue structured test data.
            payable_account=Account.objects.get(entity=entity, code="2100"),  # Fetch test database data.
            default_expense_account=Account.objects.get(entity=entity, code="5300"),  # Fetch test database data.
        )  # Close the grouped test expression.
        self.fake = FakeProvider(secret="test-secret")  # Assign test setup data.
        # Resolve the default provider name and the fake's own name to this instance.
        registry.register("PAYSTACK", self.fake)  # Execute the test step.
        registry.register("FAKE", self.fake)  # Execute the test step.
        self.addCleanup(registry.unregister)  # Execute the test step.
        return entity, customer, vendor  # Return the prepared test value.

    def make_posted_invoice(self, entity, customer, *, amount):  # Define a test helper or test method.
        inv = Invoice.objects.create(  # Create test database data.
            entity=entity, customer=customer,  # Continue structured test data.
            invoice_date=datetime.date.today(), due_date=datetime.date.today(),  # Continue structured test data.
        )  # Close the grouped test expression.
        InvoiceLine.objects.create(  # Create test database data.
            invoice=inv, revenue_account=Account.objects.get(entity=entity, code="4100"),  # Fetch test database data.
            quantity=1, unit_price=amount, tax_code=None, line_no=1,  # Continue structured test data.
        )  # Close the grouped test expression.
        post_invoice(inv)  # Execute the test step.
        return inv  # Return the prepared test value.


class ProviderTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_fake_signature_roundtrip(self):  # Define a test helper or test method.
        self.build()  # Execute the test step.
        raw, headers = self.fake.build_webhook(  # Continue structured test data.
            event="charge.success", reference="R1", status="SUCCEEDED", amount=1000,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertTrue(self.fake.verify_signature(raw_body=raw, headers=headers))  # Check the expected test outcome.
        # Tamper the body → signature no longer matches.
        self.assertFalse(self.fake.verify_signature(raw_body=raw + b"x", headers=headers))  # Check the expected test outcome.

    def test_registry_override_resolves_fake(self):  # Define a test helper or test method.
        self.build()  # Execute the test step.
        self.assertIs(registry.get_provider("PAYSTACK"), self.fake)  # Check the expected test outcome.


class CollectionTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_initiate_creates_processing_intent_with_checkout(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(  # Continue structured test data.
            entity=entity, amount=50000, customer=customer, narration="Fees",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)  # Check the expected test outcome.
        self.assertTrue(intent.checkout_url)  # Check the expected test outcome.
        self.assertTrue(intent.provider_reference)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            PaymentEvent.objects.filter(  # Query test database data.
                action="COLLECTION_INITIATED", reference=intent.reference, succeeded=True,  # Continue structured test data.
            ).exists()  # Execute the test step.
        )  # Close the grouped test expression.

    def test_confirm_via_verify_books_receipt_and_pays_invoice(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        inv = self.make_posted_invoice(entity, customer, amount=50000)  # Assign test setup data.
        intent = services.initiate_collection(  # Continue structured test data.
            entity=entity, amount=50000, customer=customer, invoice=inv,  # Continue structured test data.
        )  # Close the grouped test expression.
        # Provider now reports success; confirm with no explicit status polls verify().
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # Assign test setup data.
        intent = services.confirm_collection(intent)  # Assign test setup data.

        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)  # Check the expected test outcome.
        self.assertIsNotNone(intent.payment_id)  # Check the expected test outcome.
        payment = Payment.objects.get(pk=intent.payment_id)  # Fetch test database data.
        self.assertEqual(payment.amount, 50000)  # Check the expected test outcome.
        self.assertEqual(payment.status, "POSTED")  # Check the expected test outcome.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_paid, 50000)  # Check the expected test outcome.

    def test_failed_collection_books_nothing(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=9999, customer=customer)  # Assign test setup data.
        intent = services.confirm_collection(intent, status=CollectionStatus.FAILED)  # Assign test setup data.
        self.assertEqual(intent.status, CollectionStatus.FAILED)  # Check the expected test outcome.
        self.assertIsNone(intent.payment_id)  # Check the expected test outcome.
        self.assertFalse(Payment.objects.filter(entity=entity).exists())  # Check the expected test outcome.

    def test_confirm_is_idempotent(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=20000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        # A second confirm must not book a second receipt.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)  # Check the expected test outcome.

    def test_settled_amount_overrides_requested(self):  # Define a test helper or test method.
        """A provider that settles less than requested books the settled amount and
        keeps the requested amount for audit."""
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=50000, customer=customer)  # Assign test setup data.
        intent = services.confirm_collection(  # Continue structured test data.
            intent, status=CollectionStatus.SUCCEEDED, amount=48000,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(intent.amount, 48000)  # Check the expected test outcome.
        payment = Payment.objects.get(pk=intent.payment_id)  # Fetch test database data.
        self.assertEqual(payment.amount, 48000)  # Check the expected test outcome.
        self.assertEqual(intent.metadata["requested_amount"], 50000)  # Check the expected test outcome.

    def test_standalone_receipt_parks_credit_not_auto_settling(self):  # Define a test helper or test method.
        """A collection with no invoice must NOT auto-settle the customer's open
        invoices; the cash parks as customer credit instead."""
        entity, customer, _ = self.build()  # Assign test setup data.
        inv = self.make_posted_invoice(entity, customer, amount=30000)  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=20000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        inv.refresh_from_db()  # Execute the test step.
        self.assertEqual(inv.amount_paid, 0)  # not auto-settled
        self.assertGreater(customer_credit_balance(customer), 0)  # cash held as credit

    def test_customerless_initiate_is_rejected(self):  # Define a test helper or test method.
        entity, _, _ = self.build()  # Assign test setup data.
        with self.assertRaises(ValidationError):  # Enter a test context manager.
            services.initiate_collection(entity=entity, amount=1000)  # Assign test setup data.
        self.assertFalse(CollectionIntent.objects.filter(entity=entity).exists())  # Check the expected test outcome.

    def test_one_active_virtual_account_per_customer_provider(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")  # Assign test setup data.
        with self.assertRaises(ValidationError):  # Enter a test context manager.
            services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")  # Assign test setup data.
        self.assertEqual(  # Check the expected test outcome.
            VirtualAccount.objects.filter(entity=entity, customer=customer).count(), 1)  # Query test database data.

    def test_inactive_virtual_account_deposit_is_held(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")  # Assign test setup data.
        services.set_virtual_account_status(va, status="INACTIVE")  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=25000, customer=customer)  # Assign test setup data.
        intent.virtual_account = va  # Assign test setup data.
        intent.save(update_fields=["virtual_account"])  # Assign test setup data.
        with self.assertRaises(PaymentStateError):  # Enter a test context manager.
            services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        self.assertFalse(Payment.objects.filter(entity=entity).exists())  # Check the expected test outcome.


class WebhookTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _signed(self, *, event, reference, status, amount=0):  # Define a test helper or test method.
        return self.fake.build_webhook(  # Return the prepared test value.
            event=event, reference=reference, status=status, amount=amount,  # Continue structured test data.
        )  # Close the grouped test expression.

    def test_bad_signature_is_rejected_and_books_nothing(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=30000, customer=customer)  # Assign test setup data.
        raw, _ = self._signed(event="charge.success", reference=intent.reference, status="SUCCEEDED")  # Assign test setup data.
        with self.assertRaises(WebhookSignatureError):  # Enter a test context manager.
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw,  # Continue structured test data.
                                    headers={"x-fake-signature": "deadbeef"})  # Assign test setup data.
        intent.refresh_from_db()  # Execute the test step.
        self.assertNotEqual(intent.status, CollectionStatus.SUCCEEDED)  # Check the expected test outcome.
        self.assertFalse(Payment.objects.filter(entity=entity).exists())  # Check the expected test outcome.

    def test_webhook_confirms_collection(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        inv = self.make_posted_invoice(entity, customer, amount=40000)  # Assign test setup data.
        intent = services.initiate_collection(  # Continue structured test data.
            entity=entity, amount=40000, customer=customer, invoice=inv,  # Continue structured test data.
        )  # Close the grouped test expression.
        # The webhook only triggers re-verification; the provider's API is the source
        # of truth for status, so make verify agree the collection succeeded.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # Assign test setup data.
        raw, headers = self._signed(  # Continue structured test data.
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=40000,  # Continue structured test data.
        )  # Close the grouped test expression.
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)  # Assign test setup data.
        self.assertEqual(event.status, "PROCESSED")  # Check the expected test outcome.
        intent.refresh_from_db()  # Execute the test step.
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)  # Check the expected test outcome.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)  # Check the expected test outcome.

    def test_duplicate_webhook_never_double_books(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=25000, customer=customer)  # Assign test setup data.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # provider verify agrees
        raw, headers = self._signed(  # Continue structured test data.
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=25000,  # Continue structured test data.
        )  # Close the grouped test expression.
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)  # Assign test setup data.
        # The provider retries the exact same event.
        with self.assertRaises(DuplicateWebhookError):  # Enter a test context manager.
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)  # Assign test setup data.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)  # Check the expected test outcome.

    def test_webhook_does_not_book_when_provider_verify_disagrees(self):  # Define a test helper or test method.
        # SECURITY: a validly-signed "charge.success" must NOT book a receipt if the
        # provider's own API doesn't confirm the transaction settled. The event is only
        # a trigger to re-verify — here verify returns PENDING (no forced_status), so
        # nothing is booked despite the webhook claiming SUCCEEDED.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=30000, customer=customer)  # Assign test setup data.
        raw, headers = self._signed(  # Continue structured test data.
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=30000,  # Continue structured test data.
        )  # Close the grouped test expression.
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)  # Assign test setup data.
        intent.refresh_from_db()  # Execute the test step.
        self.assertNotEqual(intent.status, CollectionStatus.SUCCEEDED)  # Check the expected test outcome.
        self.assertFalse(Payment.objects.filter(entity=entity).exists())  # Check the expected test outcome.


class PayoutTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_initiate_then_confirm_books_vendor_payment(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        payout = services.initiate_payout(  # Continue structured test data.
            entity=entity, amount=70000, beneficiary_name="Supplier Ltd",  # Continue structured test data.
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",  # Continue structured test data.
            vendor=vendor, narration="Settle bill",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)  # Check the expected test outcome.
        payout = services.confirm_payout(payout, status=PayoutStatus.PAID)  # Assign test setup data.
        self.assertEqual(payout.status, PayoutStatus.PAID)  # Check the expected test outcome.
        self.assertIsNotNone(payout.vendor_payment_id)  # Check the expected test outcome.
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)  # Fetch test database data.
        self.assertEqual(vp.gross_amount, 70000)  # Check the expected test outcome.
        self.assertEqual(vp.status, "POSTED")  # Check the expected test outcome.

    def test_payout_webhook_confirms(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        payout = services.initiate_payout(  # Continue structured test data.
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",  # Continue structured test data.
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",  # Continue structured test data.
            vendor=vendor,  # Continue structured test data.
        )  # Close the grouped test expression.
        # Webhook triggers re-verification; provider verify_transfer is the source of truth.
        self.fake.forced_status[payout.reference] = "PAID"  # Assign test setup data.
        raw, headers = self.fake.build_webhook(  # Continue structured test data.
            event="transfer.success", reference=payout.reference, status="PAID", amount=15000,  # Continue structured test data.
        )  # Close the grouped test expression.
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)  # Assign test setup data.
        payout.refresh_from_db()  # Execute the test step.
        self.assertEqual(payout.status, PayoutStatus.PAID)  # Check the expected test outcome.
        self.assertEqual(VendorPayment.objects.filter(entity=entity).count(), 1)  # Check the expected test outcome.

    def test_failed_payout_books_nothing(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        payout = services.initiate_payout(  # Continue structured test data.
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",  # Continue structured test data.
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",  # Continue structured test data.
            vendor=vendor,  # Continue structured test data.
        )  # Close the grouped test expression.
        payout = services.confirm_payout(payout, status=PayoutStatus.FAILED)  # Assign test setup data.
        self.assertEqual(payout.status, PayoutStatus.FAILED)  # Check the expected test outcome.
        self.assertIsNone(payout.vendor_payment_id)  # Check the expected test outcome.
        self.assertFalse(VendorPayment.objects.filter(entity=entity).exists())  # Check the expected test outcome.


class _FlakyProvider(FakeProvider):  # Define a test fixture or test case class.
    """A FakeProvider that refuses to transfer a sentinel amount (to fail one item)."""

    def __init__(self, *, fail_amount, **kwargs):  # Define a test helper or test method.
        super().__init__(**kwargs)  # Execute the test step.
        self.fail_amount = fail_amount  # Fail the test when the invariant is violated.

    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,  # Define a test helper or test method.
                        account_name="", narration="", metadata=None):  # Start the nested test block.
        if amount == self.fail_amount:  # Branch test setup or assertions.
            from .exceptions import ProviderError  # Import project symbols exercised by these tests.
            raise ProviderError("Beneficiary account rejected by the bank.")  # Raise the expected test exception.
        return super().create_transfer(  # Return the prepared test value.
            reference=reference, amount=amount, currency=currency,  # Continue structured test data.
            account_number=account_number, bank_code=bank_code,  # Continue structured test data.
            account_name=account_name, narration=narration, metadata=metadata,  # Continue structured test data.
        )  # Close the grouped test expression.


class PayoutBatchTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _items(self, vendor, *amounts):  # Define a test helper or test method.
        return [  # Return the prepared test value.
            {"amount": amt, "beneficiary_name": "Supplier Ltd",  # Continue structured test data.
             "beneficiary_account_number": f"012345678{i}", "beneficiary_bank_code": "058",  # Continue structured test data.
             "vendor": vendor}  # Execute the test step.
            for i, amt in enumerate(amounts)  # Iterate through test data.
        ]  # Close the grouped test expression.

    def test_create_batch_assembles_pending_instructions_without_submitting(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        batch = services.create_payout_batch(  # Continue structured test data.
            entity=entity, items=self._items(vendor, 10000, 20000, 30000),  # Continue structured test data.
            title="June payroll",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)  # Check the expected test outcome.
        self.assertEqual(batch.item_count, 3)  # Check the expected test outcome.
        self.assertEqual(batch.total_amount, 60000)  # Check the expected test outcome.
        self.assertEqual(batch.instructions.count(), 3)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            all(p.status == PayoutStatus.PENDING for p in batch.instructions.all())  # Execute the test step.
        )  # Close the grouped test expression.
        # Nothing was sent to the provider yet → no provider_reference.
        self.assertTrue(all(not p.provider_reference for p in batch.instructions.all()))  # Check the expected test outcome.

    def test_submit_batch_dispatches_every_item(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))  # Assign test setup data.
        batch = services.submit_payout_batch(batch)  # Assign test setup data.
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)  # Check the expected test outcome.
        self.assertIsNotNone(batch.submitted_at)  # Check the expected test outcome.
        self.assertTrue(  # Check the expected test outcome.
            all(p.status == PayoutStatus.PROCESSING for p in batch.instructions.all())  # Execute the test step.
        )  # Close the grouped test expression.

    def test_confirming_all_items_completes_the_batch(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))  # Assign test setup data.
        services.submit_payout_batch(batch)  # Execute the test step.
        for payout in batch.instructions.all():  # Iterate through test data.
            services.confirm_payout(payout, status=PayoutStatus.PAID)  # Assign test setup data.
        batch.refresh_from_db()  # Execute the test step.
        self.assertEqual(batch.status, PayoutBatchStatus.COMPLETED)  # Check the expected test outcome.
        self.assertEqual(  # Check the expected test outcome.
            VendorPayment.objects.filter(entity=entity).count(), 2  # Query test database data.
        )  # Close the grouped test expression.

    def test_partial_failure_marks_batch_partially_completed(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        flaky = _FlakyProvider(secret="test-secret", fail_amount=7000)  # Assign test setup data.
        registry.register("PAYSTACK", flaky)  # Execute the test step.
        registry.register("FAKE", flaky)  # Execute the test step.
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))  # Assign test setup data.
        services.submit_payout_batch(batch)  # Execute the test step.
        # One dispatched (PROCESSING), one rejected at submit (FAILED).
        statuses = sorted(p.status for p in batch.instructions.all())  # Assign test setup data.
        self.assertEqual(statuses, [PayoutStatus.FAILED, PayoutStatus.PROCESSING])  # Check the expected test outcome.
        batch.refresh_from_db()  # Execute the test step.
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)  # Check the expected test outcome.
        # Confirm the surviving item → batch settles as PARTIALLY_COMPLETED.
        survivor = batch.instructions.get(status=PayoutStatus.PROCESSING)  # Assign test setup data.
        services.confirm_payout(survivor, status=PayoutStatus.PAID)  # Assign test setup data.
        batch.refresh_from_db()  # Execute the test step.
        self.assertEqual(batch.status, PayoutBatchStatus.PARTIALLY_COMPLETED)  # Check the expected test outcome.


class SettlementReconciliationTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def _bank_account(self, entity):  # Define a test helper or test method.
        from vs_finance.models import BankAccount  # Import project symbols exercised by these tests.
        return BankAccount.objects.create(  # Return the prepared test value.
            entity=entity, name="Operations",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
        )  # Close the grouped test expression.

    def _bank_line(self, bank_account, *, amount, reference="", description="", day=None):  # Define a test helper or test method.
        from vs_finance.models import BankStatementLine  # Import project symbols exercised by these tests.
        return BankStatementLine.objects.create(  # Return the prepared test value.
            bank_account=bank_account, txn_date=day or datetime.date.today(),  # Continue structured test data.
            description=description, reference=reference, amount=amount,  # Continue structured test data.
        )  # Close the grouped test expression.

    def test_reference_match_settles_a_collection(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        intent.refresh_from_db()  # Execute the test step.
        ba = self._bank_account(entity)  # Assign test setup data.
        self._bank_line(ba, amount=40000, reference=intent.reference)  # Assign test setup data.

        recon = reconciliation.settlement_reconciliation(entity)  # Assign test setup data.
        self.assertEqual(recon.settled_count, 1)  # Check the expected test outcome.
        self.assertEqual(recon.unsettled_count, 0)  # Check the expected test outcome.
        self.assertEqual(recon.rows[0].match_basis, "reference")  # Check the expected test outcome.
        self.assertTrue(recon.is_reconciled)  # Check the expected test outcome.

    def test_matched_row_carries_net_and_fee_from_the_bank_line(self):  # Define a test helper or test method.
        """A collection booked at gross that settles to the bank net-of-fee exposes the
        PSP fee (gross − net) and the bank settlement reference on the matched row."""
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        intent.refresh_from_db()  # Execute the test step.
        ba = self._bank_account(entity)  # Assign test setup data.
        # Bank received 39,100 net of a 900 PSP fee, under a settlement reference.
        self._bank_line(ba, amount=39100, reference=intent.reference, description="STL-PSK-0001")  # Assign test setup data.

        row = reconciliation.settlement_reconciliation(entity).rows[0]  # Assign test setup data.
        self.assertEqual(row.amount, 40000)         # gross (gateway)
        self.assertEqual(row.settled_amount, 39100)  # net (bank)
        self.assertEqual(row.fee_amount, 900)        # PSP fee
        self.assertEqual(row.settlement_reference, intent.reference)  # Check the expected test outcome.

    def test_amount_fallback_match_for_a_payout(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        payout = services.initiate_payout(  # Continue structured test data.
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",  # Continue structured test data.
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",  # Continue structured test data.
            vendor=vendor,  # Continue structured test data.
        )  # Close the grouped test expression.
        services.confirm_payout(payout, status=PayoutStatus.PAID)  # Assign test setup data.
        ba = self._bank_account(entity)  # Assign test setup data.
        # No shared reference, but the signed amount matches (-15000 outflow).
        self._bank_line(ba, amount=-15000, reference="GTB-REF-XYZ")  # Assign test setup data.

        recon = reconciliation.settlement_reconciliation(entity)  # Assign test setup data.
        self.assertEqual(recon.settled_count, 1)  # Check the expected test outcome.
        self.assertEqual(recon.rows[0].amount, -15000)  # Check the expected test outcome.
        self.assertEqual(recon.rows[0].match_basis, "amount")  # Check the expected test outcome.
        self.assertTrue(recon.is_reconciled)  # Check the expected test outcome.

    def test_unsettled_gateway_and_unexplained_bank_line_break_reconciliation(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=22000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        ba = self._bank_account(entity)  # Assign test setup data.
        # A bank line that matches nothing (wrong amount, no shared reference).
        self._bank_line(ba, amount=99999, reference="MYSTERY", description="Bank charge")  # Assign test setup data.

        recon = reconciliation.settlement_reconciliation(entity)  # Assign test setup data.
        self.assertEqual(recon.unsettled_count, 1)  # Check the expected test outcome.
        self.assertEqual(len(recon.unmatched_bank_lines), 1)  # Check the expected test outcome.
        self.assertFalse(recon.is_reconciled)  # Check the expected test outcome.

    def test_date_window_filters_both_sides(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=12000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        ba = self._bank_account(entity)  # Assign test setup data.
        self._bank_line(ba, amount=12000, reference=intent.reference)  # Assign test setup data.
        # A window entirely in the past excludes today's confirmation and bank line.
        past = datetime.date.today() - datetime.timedelta(days=10)  # Assign test setup data.
        recon = reconciliation.settlement_reconciliation(  # Continue structured test data.
            entity, start_date=past - datetime.timedelta(days=5), end_date=past,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(recon.rows, [])  # Check the expected test outcome.
        self.assertEqual(recon.unmatched_bank_lines, [])  # Check the expected test outcome.
        self.assertTrue(recon.is_reconciled)  # Check the expected test outcome.


class PaymentEventTests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    def test_payment_event_is_append_only(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        services.initiate_collection(entity=entity, amount=10000, customer=customer)  # Assign test setup data.
        ev = PaymentEvent.objects.filter(action="COLLECTION_INITIATED").first()  # Query test database data.
        self.assertIsNotNone(ev)  # Check the expected test outcome.
        ev.message = "tampered"  # Assign test setup data.
        with self.assertRaises(ValueError):  # Enter a test context manager.
            ev.save()  # Execute the test step.
        with self.assertRaises(ValueError):  # Enter a test context manager.
            ev.delete()  # Execute the test step.


class PaymentsAPITests(_PaymentsFixtureMixin, TestCase):  # Define a test fixture or test case class.
    """The /v1/payments/ REST surface, authenticated as a Vision super admin."""

    def setUp(self):  # Define a test helper or test method.
        from django.contrib.auth import get_user_model  # Import project symbols exercised by these tests.
        from rest_framework.test import APIClient  # Import project symbols exercised by these tests.
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment  # Import project symbols exercised by these tests.

        User = get_user_model()  # Assign test setup data.
        self.user = User.objects.create_user(  # Create test database data.
            email="pay-admin@test.com", password="testpass123",  # Continue structured test data.
            user_type="CX_STAFF", status="ACTIVE",  # Continue structured test data.
            first_name="Pay", last_name="Admin",  # Continue structured test data.
        )  # Close the grouped test expression.
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")  # Create test database data.
        PlatformUserRoleAssignment.objects.create(  # Create test database data.
            user=self.user, role=role, assignment_status="ACTIVE",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.client = APIClient()  # Assign test setup data.
        self.client.force_authenticate(user=self.user)  # Exercise the test HTTP client.

    def test_initiate_collection_endpoint(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/payments/collections/?entity={entity.code}",  # Continue structured test data.
            {"amount": 50000, "customer": customer.pk, "narration": "Fees"},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["status"], CollectionStatus.PROCESSING)  # Check the expected test outcome.
        self.assertTrue(data["checkout_url"])  # Check the expected test outcome.

    def test_collection_detail_verify_confirms(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=12000, customer=customer)  # Assign test setup data.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # Assign test setup data.
        resp = self.client.get(  # Exercise the test HTTP client.
            f"/v1/payments/collections/{intent.pk}/?entity={entity.code}&verify=1"  # Assign test setup data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        self.assertEqual(resp.json()["data"]["status"], CollectionStatus.SUCCEEDED)  # Check the expected test outcome.

    def test_webhook_endpoint_processes_and_dedupes(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=33000, customer=customer)  # Assign test setup data.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # provider verify agrees
        raw, headers = self.fake.build_webhook(  # Continue structured test data.
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=33000,  # Continue structured test data.
        )  # Close the grouped test expression.
        sig = headers["x-fake-signature"]  # Assign test setup data.
        first = self.client.post(  # Exercise the test HTTP client.
            "/v1/payments/webhooks/PAYSTACK/", data=raw,  # Continue structured test data.
            content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(first.status_code, 200)  # Check the expected test outcome.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)  # Check the expected test outcome.
        # A retry is acknowledged (200) but does not re-book.
        second = self.client.post(  # Exercise the test HTTP client.
            "/v1/payments/webhooks/PAYSTACK/", data=raw,  # Continue structured test data.
            content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(second.status_code, 200)  # Check the expected test outcome.
        self.assertTrue(second.json()["data"].get("duplicate"))  # Check the expected test outcome.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)  # Check the expected test outcome.

    def test_initiate_payout_endpoint(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/payments/payouts/?entity={entity.code}",  # Continue structured test data.
            {"amount": 60000, "beneficiary_name": "Supplier Ltd",  # Continue structured test data.
             "beneficiary_account_number": "0123456789", "beneficiary_bank_code": "058",  # Continue structured test data.
             "vendor": vendor.pk},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        self.assertEqual(resp.json()["data"]["status"], PayoutStatus.PROCESSING)  # Check the expected test outcome.

    def test_create_and_submit_payout_batch_endpoint(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        resp = self.client.post(  # Exercise the test HTTP client.
            f"/v1/payments/payout-batches/?entity={entity.code}",  # Continue structured test data.
            {"title": "Run", "submit": True,  # Continue structured test data.
             "items": [  # Continue structured test data.
                 {"amount": 11000, "beneficiary_name": "Supplier Ltd",  # Continue structured test data.
                  "beneficiary_account_number": "0123456789", "beneficiary_bank_code": "058",  # Continue structured test data.
                  "vendor": vendor.pk},  # Continue structured test data.
                 {"amount": 22000, "beneficiary_name": "Supplier Ltd",  # Continue structured test data.
                  "beneficiary_account_number": "0123456780", "beneficiary_bank_code": "058",  # Continue structured test data.
                  "vendor": vendor.pk},  # Continue structured test data.
             ]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 201)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertEqual(data["status"], PayoutBatchStatus.PROCESSING)  # Check the expected test outcome.
        self.assertEqual(data["item_count"], 2)  # Check the expected test outcome.
        self.assertEqual(data["total_amount"], 33000)  # Check the expected test outcome.
        self.assertEqual(len(data["instructions"]), 2)  # Check the expected test outcome.

    def test_batch_endpoint_resolves_vendor_by_code_and_requires_one(self):  # Define a test helper or test method.
        entity, _, vendor = self.build()  # Assign test setup data.
        # vendor by CODE (the picker emits codes) + per-line WHT
        ok = self.client.post(  # Exercise the test HTTP client.
            f"/v1/payments/payout-batches/?entity={entity.code}",  # Continue structured test data.
            {"title": "By code", "items": [  # Continue structured test data.
                {"amount": 40000, "wht_amount": 4000, "beneficiary_name": "Supplier Ltd",  # Continue structured test data.
                 "beneficiary_account_number": "0123456789", "vendor": vendor.code},  # Continue structured test data.
            ]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(ok.status_code, 201, ok.content)  # Check the expected test outcome.
        self.assertEqual(ok.json()["data"]["instructions"][0]["wht_amount"], 4000)  # Check the expected test outcome.
        # a line with no vendor is rejected (it could never book)
        bad = self.client.post(  # Exercise the test HTTP client.
            f"/v1/payments/payout-batches/?entity={entity.code}",  # Continue structured test data.
            {"items": [{"amount": 40000, "beneficiary_name": "X",  # Continue structured test data.
                        "beneficiary_account_number": "0123456789"}]},  # Continue structured test data.
            format="json",  # Continue structured test data.
        )  # Close the grouped test expression.
        self.assertEqual(bad.status_code, 400, bad.content)  # Check the expected test outcome.

    def test_settlement_reconciliation_endpoint(self):  # Define a test helper or test method.
        from vs_finance.models import BankAccount, BankStatementLine  # Import project symbols exercised by these tests.

        entity, customer, _ = self.build()  # Assign test setup data.
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)  # Assign test setup data.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)  # Assign test setup data.
        intent.refresh_from_db()  # Execute the test step.
        ba = BankAccount.objects.create(  # Create test database data.
            entity=entity, name="Operations",  # Continue structured test data.
            gl_account=Account.objects.get(entity=entity, code="1100"),  # Fetch test database data.
        )  # Close the grouped test expression.
        BankStatementLine.objects.create(  # Create test database data.
            bank_account=ba, txn_date=datetime.date.today(),  # Continue structured test data.
            reference=intent.reference, amount=40000,  # Continue structured test data.
        )  # Close the grouped test expression.
        resp = self.client.get(  # Exercise the test HTTP client.
            f"/v1/payments/reports/settlement-reconciliation/?entity={entity.code}"  # Assign test setup data.
        )  # Close the grouped test expression.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        data = resp.json()["data"]  # Assign test setup data.
        self.assertTrue(data["is_reconciled"])  # Check the expected test outcome.
        self.assertEqual(data["summary"]["settled_count"], 1)  # Check the expected test outcome.

    def test_transactions_log_endpoint(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        # Initiating a collection writes a COLLECTION_INITIATED PaymentEvent.
        services.initiate_collection(entity=entity, amount=40000, customer=customer)  # Assign test setup data.
        resp = self.client.get(f"/v1/payments/transactions/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        rows = resp.json()["data"]  # Assign test setup data.
        self.assertTrue(any(r["action"] == "COLLECTION_INITIATED" for r in rows))  # Check the expected test outcome.
        self.assertIn("action_display", rows[0])  # Check the expected test outcome.
        # The ?action= filter narrows the log.
        filtered = self.client.get(  # Exercise the test HTTP client.
            f"/v1/payments/transactions/?entity={entity.code}&action=PAYOUT_FAILED"  # Assign test setup data.
        )  # Close the grouped test expression.
        self.assertEqual(filtered.status_code, 200)  # Check the expected test outcome.
        # success_response coerces an empty list to {}, so assert it's falsy.
        self.assertFalse(filtered.json()["data"])  # Check the expected test outcome.

    def test_virtual_account_provision_list_and_status(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.

        # Provision via the Fake provider → mints a test NUBAN.
        created = self.client.post(  # Exercise the test HTTP client.
            f"/v1/payments/virtual-accounts/?entity={entity.code}",  # Continue structured test data.
            {"customer": customer.pk, "provider": "FAKE"}, format="json")  # Assign test setup data.
        self.assertEqual(created.status_code, 201, created.content)  # Check the expected test outcome.
        va = created.json()["data"]  # Assign test setup data.
        self.assertEqual(va["status"], "ACTIVE")  # Check the expected test outcome.
        self.assertEqual(va["customer_code"], "CUST1")  # Check the expected test outcome.
        # super-admin holds view_sensitive → the funding number is visible.
        self.assertTrue(va["account_number"])  # Check the expected test outcome.

        # GET list is paginated and rides KPIs.
        listed = self.client.get(f"/v1/payments/virtual-accounts/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(listed.status_code, 200)  # Check the expected test outcome.
        body = listed.json()  # Assign test setup data.
        self.assertEqual(body["kpis"], {"total": 1, "active": 1, "inactive": 0, "providers": 1})  # Check the expected test outcome.
        self.assertEqual(body["pagination"]["totalItems"], 1)  # Check the expected test outcome.
        self.assertEqual(len(body["data"]), 1)  # Check the expected test outcome.

        # Status filter excludes the active one.
        inactive_list = self.client.get(  # Exercise the test HTTP client.
            f"/v1/payments/virtual-accounts/?entity={entity.code}&status=INACTIVE")  # Assign test setup data.
        self.assertFalse(inactive_list.json()["data"])  # Check the expected test outcome.

        # PATCH deactivates it.
        patched = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/payments/virtual-accounts/{va['id']}/?entity={entity.code}",  # Continue structured test data.
            {"status": "INACTIVE"}, format="json")  # Assign test setup data.
        self.assertEqual(patched.status_code, 200, patched.content)  # Check the expected test outcome.
        self.assertEqual(patched.json()["data"]["status"], "INACTIVE")  # Check the expected test outcome.
        # KPIs reflect it.
        kpis = self.client.get(  # Exercise the test HTTP client.
            f"/v1/payments/virtual-accounts/?entity={entity.code}").json()["kpis"]  # Assign test setup data.
        self.assertEqual(kpis, {"total": 1, "active": 0, "inactive": 1, "providers": 1})  # Check the expected test outcome.

        # A bogus status is rejected.
        bad = self.client.patch(  # Exercise the test HTTP client.
            f"/v1/payments/virtual-accounts/{va['id']}/?entity={entity.code}",  # Continue structured test data.
            {"status": "SUSPENDED"}, format="json")  # Assign test setup data.
        self.assertEqual(bad.status_code, 400, bad.content)  # Check the expected test outcome.

    def test_virtual_account_list_uses_standard_envelope(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")  # Assign test setup data.
        resp = self.client.get(f"/v1/payments/virtual-accounts/?entity={entity.code}")  # Exercise the test HTTP client.
        self.assertEqual(resp.status_code, 200)  # Check the expected test outcome.
        body = resp.json()  # Assign test setup data.
        self.assertEqual(body["pagination"]["pageSize"], 25)  # Check the expected test outcome.
        self.assertIn("kpis", body)  # Check the expected test outcome.
        self.assertEqual(len(body["data"]), 1)  # Check the expected test outcome.

    def test_collections_filter_by_virtual_account(self):  # Define a test helper or test method.
        entity, customer, _ = self.build()  # Assign test setup data.
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")  # Assign test setup data.
        # A collection that arrived through that VA.
        intent = services.initiate_collection(entity=entity, amount=40000, customer=customer)  # Assign test setup data.
        intent.virtual_account = va  # Assign test setup data.
        intent.save(update_fields=["virtual_account"])  # Assign test setup data.
        # Another, not linked to the VA.
        services.initiate_collection(entity=entity, amount=10000, customer=customer)  # Assign test setup data.

        scoped = self.client.get(  # Exercise the test HTTP client.
            f"/v1/payments/collections/?entity={entity.code}&virtual_account={va.id}")  # Assign test setup data.
        self.assertEqual(scoped.status_code, 200)  # Check the expected test outcome.
        rows = scoped.json()["data"]  # Assign test setup data.
        self.assertEqual([r["id"] for r in rows], [intent.id])  # Check the expected test outcome.
