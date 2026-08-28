"""Phase 6 tests - vs_payments gateway (collections + payouts + webhooks).

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
def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


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
            bank_name="Guarantee Trust Bank", bank_code="058",
            bank_account_name="Supplier Ltd", bank_account_number="0123456789",
            kyc_status="VERIFIED",
        )
        self.fake = FakeProvider(secret="test-secret")
        # Resolve the default provider name and the fake's own name to this instance.
        registry.register("PAYSTACK", self.fake)
        registry.register("FAKE", self.fake)
        self.addCleanup(registry.unregister)
        return entity, customer, vendor

    def make_processing_payout(self, entity, vendor, *, amount, narration=""):
        """Create an already-dispatched gateway row for confirmation/read-side tests."""
        reference = services._new_reference(entity)
        return PayoutInstruction.objects.create(
            entity=entity, provider="PAYSTACK", reference=reference,
            provider_reference=f"FAKE-TR-{reference}", amount=amount,
            currency=entity.base_currency,
            beneficiary_name=vendor.bank_account_name,
            beneficiary_account_number=vendor.bank_account_number,
            beneficiary_bank_code=vendor.bank_code,
            narration=narration, status=PayoutStatus.PROCESSING,
            vendor_source_type="vs_procurement.Vendor",
            vendor_source_id=str(vendor.pk),
        )

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
        self.assertEqual(
            intent.reference,
            f"CXP-{entity.tenant_id}{timezone.localdate():%y%m%d}1",
        )
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

    # Verify a gateway receipt for a not-yet-raised invoice still books, as credit.
    def test_collection_for_a_future_dated_invoice_parks_as_credit(self):
        """The payer's money has already moved, so the booking must never fail.

        A receipt cannot settle an invoice that is not raised yet - the posting
        service refuses an explicit allocation that would credit AR first. Here that
        refusal must not surface: raising would leave real cash unrecorded while the
        PSP retries a call that can never succeed. It is a prepayment, so it parks as
        customer credit and applies once the invoice exists.
        """
        from vs_finance.receivables import customer_credit_balance

        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=50000)
        future = datetime.date.today() + datetime.timedelta(days=14)
        Invoice.objects.filter(pk=inv.pk).update(invoice_date=future, due_date=future)
        inv.refresh_from_db()

        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, invoice=inv,
        )
        intent = services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)

        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        payment = Payment.objects.get(pk=intent.payment_id)
        self.assertEqual(payment.status, "POSTED")
        self.assertEqual(payment.allocated_amount, 0)  # nothing settled early
        inv.refresh_from_db()
        self.assertEqual(inv.amount_paid, 0)
        self.assertEqual(customer_credit_balance(customer), 50000)  # held as credit

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
        # The provider retries the exact same event - rejected before enqueue, so no wrapping.
        with self.assertRaises(DuplicateWebhookError):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify webhook does not book when provider verify disagrees behavior.
    def test_webhook_does_not_book_when_provider_verify_disagrees(self):
        # SECURITY: a validly-signed "charge.success" must NOT book a receipt if the
        # provider's own API doesn't confirm the transaction settled. The event is only
        # a trigger to re-verify - here verify returns PENDING (no forced_status), so
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


# Group tests for Virtual Account Deposit Tests.
class VirtualAccountDepositTests(_PaymentsFixtureMixin, TestCase):
    """Unsolicited transfers into a dedicated NUBAN have to become receipts.

    A virtual account exists so a payer can send money with no checkout at all, which
    means the deposit webhook matches no local intent. Before this the event was filed
    IGNORED and the cash sat in the bank unbooked - the whole point of the account,
    silently dropped. These tests pin the behaviour that replaces it: the account
    resolves the payer, the deposit still has to survive provider re-verification, and
    nothing about it may book twice or invent a customer.
    """

    # Support the deposit webhook workflow.
    def _deposit(self, *, account_number, reference, amount, entity=None):
        """Ingest one signed deposit event naming ``account_number`` and return the event."""
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="SUCCEEDED",
            amount=amount, receiver_account_number=account_number,
        )
        with self.captureOnCommitCallbacks(execute=True):  # Fire the deferred worker inline.
            event = webhooks.ingest_webhook(provider="FAKE", raw_body=raw, headers=headers)
        event.refresh_from_db()
        return event

    # Support the settled workflow.
    def _provider_settles(self, reference, amount):
        """Make the provider's own API agree the deposit settled for ``amount``."""
        self.fake.forced_status[reference] = "SUCCEEDED"  # Verified status, not the payload's claim.
        self.fake.forced_amount[reference] = amount  # Verified amount, not the payload's claim.

    # Verify a deposit into a known active account books one receipt behavior.
    def test_deposit_into_a_known_account_books_one_receipt_for_its_customer(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(
            entity=entity, customer=customer, provider="FAKE",
            deposit_account=Account.objects.get(entity=entity, code="1110"),
        )
        self._provider_settles("DEP-1", 45000)

        event = self._deposit(account_number=va.account_number, reference="DEP-1", amount=45000)

        self.assertEqual(event.status, "PROCESSED")
        intent = CollectionIntent.objects.get(reference="DEP-1")
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(intent.channel, "VIRTUAL_ACCOUNT")  # Not a checkout.
        self.assertEqual(intent.customer_id, customer.pk)  # The account named the payer.
        self.assertEqual(intent.virtual_account_id, va.pk)
        self.assertEqual(intent.deposit_account_id, va.deposit_account_id)
        self.assertEqual(event.collection_id, intent.pk)  # Attributable to the entity.
        payments = Payment.objects.filter(entity=entity)
        self.assertEqual(payments.count(), 1)  # Exactly one receipt.
        self.assertEqual(payments.first().amount, 45000)
        self.assertEqual(payments.first().customer_id, customer.pk)
        self.assertEqual(payments.first().status, "POSTED")
        # The account number is FLS-restricted on the virtual-account serializer, so it
        # must not be smuggled onto the receipt narration, which has no such protection.
        self.assertNotIn(va.account_number, payments.first().narration)

    # Verify a re-delivered deposit creates no second intent or receipt behavior.
    def test_a_redelivered_deposit_creates_no_second_intent_or_receipt(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        self._provider_settles("DEP-2", 30000)

        self._deposit(account_number=va.account_number, reference="DEP-2", amount=30000)
        # Webhook delivery is at-least-once: the provider sends the same event again.
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference="DEP-2", status="SUCCEEDED",
            amount=30000, receiver_account_number=va.account_number,
        )
        with self.assertRaises(DuplicateWebhookError):
            webhooks.ingest_webhook(provider="FAKE", raw_body=raw, headers=headers)

        self.assertEqual(CollectionIntent.objects.filter(reference="DEP-2").count(), 1)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify replaying a booked deposit books nothing further behavior.
    def test_replaying_a_booked_deposit_books_nothing_further(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        self._provider_settles("DEP-3", 12000)
        event = self._deposit(account_number=va.account_number, reference="DEP-3", amount=12000)

        # The operator replay path re-runs the stored body; it must be a no-op here.
        webhooks.process_stored_event(event.id)
        self.assertEqual(CollectionIntent.objects.filter(reference="DEP-3").count(), 1)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify a deposit into an inactive account is held for review behavior.
    def test_deposit_into_an_inactive_account_is_held_for_review(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        services.set_virtual_account_status(va, status=VirtualAccountStatus.INACTIVE)
        self._provider_settles("DEP-4", 20000)

        event = self._deposit(account_number=va.account_number, reference="DEP-4", amount=20000)

        self.assertEqual(event.status, "FAILED")  # Lands on the needs-attention list.
        self.assertIn("inactive", event.error.lower())
        self.assertFalse(Payment.objects.filter(entity=entity).exists())
        # The money still has to be visible: the deposit is recorded and attributable
        # even though it must not post.
        intent = CollectionIntent.objects.get(reference="DEP-4")
        self.assertEqual(intent.status, CollectionStatus.PENDING)
        self.assertEqual(intent.virtual_account_id, va.pk)
        self.assertEqual(event.collection_id, intent.pk)

    # Verify a deposit naming an unknown account stays ignored behavior.
    def test_deposit_naming_an_unknown_account_is_ignored_and_invents_nothing(self):
        entity, customer, _ = self.build()
        services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        before = Customer.objects.count()
        self._provider_settles("DEP-5", 10000)

        event = self._deposit(account_number="0000000001", reference="DEP-5", amount=10000)

        self.assertEqual(event.status, "IGNORED")
        self.assertIn("0000000001", event.error)  # The operator is told which number.
        self.assertFalse(CollectionIntent.objects.filter(reference="DEP-5").exists())
        self.assertEqual(Customer.objects.count(), before)  # No payer was invented.
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    # Verify a deposit is never booked on the event's own word behavior.
    def test_a_deposit_is_not_booked_on_the_events_word_alone(self):
        """SECURITY: the signed payload's amount must never be what moves money.

        For a checkout collection the fallback amount is the one *we* asked for, so
        trusting it is fine. A deposit intent is built from the event itself, so the
        same fallback would be the payload's own claim. A leaked webhook secret would
        then be enough to mint a receipt. With no provider-reported amount there is
        nothing authoritative to book, so the booking is refused instead.
        """
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        self.fake.forced_status["DEP-6"] = "SUCCEEDED"  # Status agrees, but no amount is reported.

        event = self._deposit(account_number=va.account_number, reference="DEP-6", amount=999999)

        self.assertEqual(event.status, "FAILED")
        self.assertIn("settled amount", event.error)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())
        self.assertEqual(
            CollectionIntent.objects.get(reference="DEP-6").status, CollectionStatus.PENDING)

    # Verify an unverified deposit books nothing behavior.
    def test_a_deposit_the_provider_will_not_confirm_books_nothing(self):
        # No forced_status: the provider's API reports PENDING despite the signed
        # "SUCCEEDED" event, so nothing may post.
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")

        self._deposit(account_number=va.account_number, reference="DEP-7", amount=15000)

        self.assertEqual(
            CollectionIntent.objects.get(reference="DEP-7").status, CollectionStatus.PENDING)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    # Verify a deposit into an account with no customer is held behavior.
    def test_deposit_into_a_customerless_account_is_recorded_but_not_booked(self):
        # VirtualAccount.customer is nullable. Without a payer there is no AR sub-ledger
        # to credit, and guessing one would be worse than an operator looking at it.
        entity, _customer, _ = self.build()
        va = VirtualAccount.objects.create(
            entity=entity, provider="FAKE", customer=None, account_number="0123456789",
            bank_name="Fake MFB", account_name="Unassigned",
        )
        self._provider_settles("DEP-8", 8000)

        event = self._deposit(account_number=va.account_number, reference="DEP-8", amount=8000)

        self.assertEqual(event.status, "FAILED")
        self.assertIn("customer", event.error.lower())
        self.assertFalse(Payment.objects.filter(entity=entity).exists())
        self.assertEqual(
            CollectionIntent.objects.get(reference="DEP-8").virtual_account_id, va.pk)

    # Verify an ordinary charge is never treated as a deposit behavior.
    def test_an_ordinary_unmatched_charge_still_reports_the_old_reason(self):
        # No destination account on the event, so the deposit path must not engage and
        # the operator-facing reason must stay the one that describes a charge.
        entity, customer, _ = self.build()
        services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")

        event = self._deposit(account_number="", reference="DEP-9", amount=5000)

        self.assertEqual(event.status, "IGNORED")
        self.assertEqual(event.error, "No matching collection intent.")
        self.assertFalse(CollectionIntent.objects.filter(reference="DEP-9").exists())


# Group tests for Payout Tests.
class PayoutTests(_PaymentsFixtureMixin, TestCase):
    def test_standalone_initiation_is_disabled(self):
        entity, _, vendor = self.build()
        with self.assertRaises(PaymentStateError):
            services.initiate_payout(
                entity=entity, amount=70000, beneficiary_name="Supplier Ltd",
                beneficiary_account_number="0123456789", beneficiary_bank_code="058",
                vendor=vendor, narration="Settle bill",
            )

    # Verify an approved provider row can still confirm and book the vendor payment.
    def test_confirm_books_vendor_payment(self):
        entity, _, vendor = self.build()
        payout = self.make_processing_payout(
            entity, vendor, amount=70000, narration="Settle bill",
        )
        payout = services.confirm_payout(payout, status=PayoutStatus.PAID)
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertIsNotNone(payout.vendor_payment_id)
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.gross_amount, 70000)
        self.assertEqual(vp.status, "POSTED")

    # Verify payout webhook confirms behavior.
    def test_payout_webhook_confirms(self):
        entity, _, vendor = self.build()
        payout = self.make_processing_payout(entity, vendor, amount=15000)
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
        payout = self.make_processing_payout(entity, vendor, amount=70000)
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
        payout = self.make_processing_payout(entity, vendor, amount=70000)
        # status supplied, amount omitted → settled falls back to the instructed 70,000.
        payout = services.confirm_payout(payout, status=PayoutStatus.PAID)
        self.assertEqual(payout.amount, 70000)
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.gross_amount, 70000)
        self.assertNotIn("instructed_amount", payout.metadata or {})

    # Verify failed payout books nothing behavior.
    def test_failed_payout_books_nothing(self):
        entity, _, vendor = self.build()
        payout = self.make_processing_payout(entity, vendor, amount=15000)
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
            {"amount": amt, "vendor": vendor}
            for amt in amounts
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
        prefix = f"CXP-{entity.tenant_id}{timezone.localdate():%y%m%d}"
        self.assertEqual(batch.reference, f"{prefix}1")
        self.assertEqual(
            set(batch.instructions.values_list("reference", flat=True)),
            {f"{prefix}{n}" for n in range(2, 5)},
        )
        self.assertTrue(
            all(p.status == PayoutStatus.PENDING for p in batch.instructions.all())
        )
        # Nothing was sent to the provider yet → no provider_reference.
        self.assertTrue(all(not p.provider_reference for p in batch.instructions.all()))

    # Verify direct service submission is closed without an approved workflow.
    def test_submit_batch_requires_approved_workflow(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))
        with self.assertRaises(PaymentStateError):
            services.submit_payout_batch(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)

    # Verify confirming all items completes the batch behavior.
    def test_confirming_all_items_completes_the_batch(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))
        batch.instructions.update(status=PayoutStatus.PROCESSING)
        batch.status = PayoutBatchStatus.PROCESSING
        batch.save(update_fields=["status", "updated_at"])
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
        first, second = list(batch.instructions.order_by("pk"))
        first.status = PayoutStatus.PROCESSING
        first.save(update_fields=["status", "updated_at"])
        second.status = PayoutStatus.FAILED
        second.save(update_fields=["status", "updated_at"])
        services._recompute_batch_status(batch)
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
        # (gross − net) would go negative - it must clamp to 0 instead.
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
        payout = self.make_processing_payout(entity, vendor, amount=15000)
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
        # A reference-matched collection - trusted, not flagged.
        c = services.initiate_collection(entity=entity, amount=40000, customer=customer)
        services.confirm_collection(c, status=CollectionStatus.SUCCEEDED)
        c.refresh_from_db()
        # An amount-only-matched payout - ambiguous, flagged.
        p = self.make_processing_payout(entity, vendor, amount=15000)
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
        self.user = User.objects.create_user(tenant=_platform_tenant(), 
            email="pay-admin@test.com", password="testpass123",
            status="ACTIVE",
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
        from vs_payments.approvals import ensure_default_approval_templates

        ensure_default_approval_templates()
        entity, _, vendor = self.build()
        resp = self.client.post(
            f"/v1/payments/payouts/?entity={entity.code}",
            {"amount": 60000, "vendor": vendor.pk},
            format="json",
            HTTP_IDEMPOTENCY_KEY="single-api-1",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["status"], PayoutStatus.PENDING)
        self.assertTrue(resp.json()["data"]["approval"]["parked"])

    # Verify create and submit payout batch endpoint behavior.
    def test_create_and_submit_payout_batch_endpoint(self):
        from vs_payments.approvals import ensure_default_approval_templates

        ensure_default_approval_templates()
        entity, _, vendor = self.build()
        resp = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"title": "Run", "submit": True,
             "items": [
                 {"amount": 11000, "vendor": vendor.pk},
                 {"amount": 22000, "vendor": vendor.pk},
             ]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="batch-api-1",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["data"]
        self.assertEqual(data["status"], PayoutBatchStatus.DRAFT)
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
                {"amount": 40000, "wht_amount": 4000, "vendor": vendor.code},
            ]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="batch-api-by-code",
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["data"]["instructions"][0]["wht_amount"], 4000)
        # a line with no vendor is rejected (it could never book)
        bad = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"items": [{"amount": 40000, "beneficiary_name": "X",
                        "beneficiary_account_number": "0123456789"}]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="batch-api-no-vendor",
        )
        self.assertEqual(bad.status_code, 400, bad.content)

    # Verify payout batch summary queued reflects only in-flight children behavior.
    def test_payout_batch_summary_queued_counts_only_in_flight_children(self):
        from vs_payments.approvals import ensure_default_approval_templates

        ensure_default_approval_templates()
        entity, _, vendor = self.build()
        flaky = _FlakyProvider(secret="test-secret", fail_amount=7000)  # Fails the 7,000 item at submit.
        registry.register("PAYSTACK", flaky)
        registry.register("FAKE", flaky)
        resp = self.client.post(
            f"/v1/payments/payout-batches/?entity={entity.code}",
            {"title": "Run", "submit": True, "items": [
                {"amount": 11000, "vendor": vendor.pk},
                {"amount": 7000, "vendor": vendor.pk},
            ]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="batch-api-summary",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        summary = self.client.get(f"/v1/payments/payout-batches/summary/?entity={entity.code}")
        self.assertEqual(summary.status_code, 200)
        # Approval-pending instructions are both queued; no provider call has happened.
        self.assertEqual(summary.json()["data"]["queued"]["kobo"], 18000)

    # Verify movements feed does not expose internal ledger ids behavior.
    def test_movements_feed_hides_internal_linked_id(self):
        entity, customer, vendor = self.build()
        services.initiate_collection(entity=entity, amount=40000, customer=customer)
        self.make_processing_payout(entity, vendor, amount=15000)
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
    """Approval-gating bulk payout batches via vs_workflow.

    A payout batch is the highest-risk cash-out path. Provider submission happens
    only after a matching workflow reaches terminal human approval, and a missing
    template fails closed.
    """

    APPROVE_ROLE = "payout-approver"

    def setUp(self):
        import io
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from rest_framework.test import APIClient
        from vs_rbac.models import (
            TenantRoleTemplate, TenantUserRoleAssignment, TenantRolePermission,
        )
        from vs_tenants.models import Tenant
        from vs_workflow.models import WorkflowTemplate

        call_command("seed_payments_permissions", verbosity=0, stdout=io.StringIO())
        # Most cases publish an explicit tenant policy, and two deliberately exercise
        # the fail-closed missing-template state. Remove the migration fallback so each
        # test controls that precondition rather than inheriting global seed data.
        WorkflowTemplate.all_objects.filter(
            document_type="payments.payout_batch",
        ).delete()

        self.User = get_user_model()
        self.TenantRoleTemplate = TenantRoleTemplate
        self.TenantRolePermission = TenantRolePermission
        self.TenantUserRoleAssignment = TenantUserRoleAssignment

        # A tenant-owned entity with one branch. The workflow scope token is legacy,
        # but approver resolution is tenant-wide for branchless payout batches.
        self.tenant = Tenant.objects.create(
            name="Cedar", slug="cedar-pba", kind=Tenant.Kind.SCHOOL,
            status=Tenant.Status.ACTIVE,
        )
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Cedar Books", code="CDRBK", kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
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
            bank_name="Guarantee Trust Bank", bank_code="058",
            bank_account_name="Supplier Ltd", bank_account_number="0123456789",
            kyc_status="VERIFIED",
        )
        self.fake = FakeProvider(secret="test-secret")
        registry.register("PAYSTACK", self.fake)
        registry.register("FAKE", self.fake)
        self.addCleanup(registry.unregister)

        # Requester: a tenant user holding every tenant-scoped payments/finance key;
        # approve verbs are excluded so separation-of-duties cases stay meaningful.
        from vs_tenants.models import Branch
        from django.db.models import Q

        from vs_rbac.models import (
            Permission,
            PermissionScope,
            TenantRolePermission,
        )
        branch = Branch.objects.create(
            tenant=self.tenant, name="Main", is_main=True, status="ACTIVE",
        )
        self.requester = self.User.objects.create_user(
            email="req-pba@test.com", password="pw", status="ACTIVE",
            first_name="Req", last_name="Ester", branch=branch,
        )
        ops_role, created = TenantRoleTemplate.objects.get_or_create(
            tenant=self.tenant, key="payments-ops-all",
            defaults={"name": "Payments Ops (all keys)", "status": "ACTIVE"},
        )
        if created:
            # Scope-filtered, not just prefix-filtered: this builds a tenant role,
            # and a handful of finance keys are platform-only because
            # they write global reference tables (currencies, FX rates). The
            # grant guard refuses those, so "every finance key" has to mean
            # every finance key a tenant may actually hold.
            keys = Permission.objects.filter(
                scope=PermissionScope.TENANT,
            ).filter(
                Q(key__startswith="payments.") | Q(key__startswith="finance.")
            )
            TenantRolePermission.objects.bulk_create(
                [TenantRolePermission(role=ops_role, permission=p)
                 for p in keys.exclude(key__endswith=".approve")],
                ignore_conflicts=True,
            )
        TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=self.requester, role=ops_role,
            assignment_status="ACTIVE",
        )
        from core.test_utils import TenantAPIClient
        self.client = TenantAPIClient(user=self.requester)

    # --- helpers ----------------------------------------------------------- #

    def _draft_batch(self, *amounts):
        items = [
            {"amount": a, "vendor": self.vendor}
            for a in (amounts or (10000,))
        ]
        return services.create_payout_batch(entity=self.entity, items=items, title="Run")

    def _ensure_approve_role(self):
        """Create the approving role, without giving it anybody.

        ``publish_template`` refuses a stage naming a role the tenant does not
        have, so the role must exist before any template is published. Having no
        holders is the parked case most of these tests are about, so creating the
        role deliberately does NOT staff it: that is ``_make_approver``'s job.
        """
        role, _ = self.TenantRoleTemplate.objects.get_or_create(
            tenant=self.tenant, key=self.APPROVE_ROLE,
            defaults={"name": "Payout Checker", "status": "ACTIVE"},
        )
        return role

    def _publish_template(self, *, on_rejection="RETURN_TO_REQUESTER"):
        from vs_workflow.services.templates import publish_template

        self._ensure_approve_role()
        return publish_template(
            tenant=self.tenant, branch=None,
            document_type="payments.payout_batch", code="standard",
            name="Payout batch approval",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_source": "ROLE",
                "approver_role_key": self.APPROVE_ROLE,
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": on_rejection, "skip_if_no_approvers": False,
            }])

    def _make_approver(self, email="apr-pba@test.com"):
        user = self.User.objects.create_user(
            email=email, password="pw", status="ACTIVE",
            first_name="Apr", last_name="Over", tenant=self.tenant,
        )
        # The stage names this role directly, so staffing it is one assignment.
        role = self._ensure_approve_role()
        self.TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=user, role=role, assignment_status="ACTIVE",
        )
        return user

    def _make_senior_approver(self, email="senior-pba@test.com"):
        from vs_payments.constants import WF_DEFAULT_HIGH_VALUE_ROLE

        user = self.User.objects.create_user(
            email=email, password="pw", status="ACTIVE",
            first_name="Senior", last_name="Checker", tenant=self.tenant,
        )
        role, _ = self.TenantRoleTemplate.objects.get_or_create(
            tenant=self.tenant, key=WF_DEFAULT_HIGH_VALUE_ROLE,
            defaults={"name": "Payout Senior Approver", "status": "ACTIVE"},
        )
        self.TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=user, role=role,
            assignment_status="ACTIVE",
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

    def _post_single(self, key=None, **overrides):
        payload = {"amount": 10_000, "vendor": self.vendor.pk} | overrides
        kwargs = {"format": "json"}
        if key is not None:
            kwargs["HTTP_IDEMPOTENCY_KEY"] = key
        return self.client.post(
            f"/v1/payments/payouts/?entity={self.entity.code}", payload, **kwargs,
        )

    def test_single_payout_requires_idempotency_key(self):
        response = self._post_single()
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_batch_creation_requires_idempotency_key(self):
        response = self.client.post(
            f"/v1/payments/payout-batches/?entity={self.entity.code}",
            {"items": [{"amount": 10_000, "vendor": self.vendor.pk}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_batch_creation_replays_same_key_without_duplicate_rows(self):
        url = f"/v1/payments/payout-batches/?entity={self.entity.code}"
        payload = {
            "title": "Supplier run",
            "items": [{"amount": 10_000, "vendor": self.vendor.pk}],
        }
        first = self.client.post(
            url, payload, format="json", HTTP_IDEMPOTENCY_KEY="batch-replay-1",
        )
        replay = self.client.post(
            url, payload, format="json", HTTP_IDEMPOTENCY_KEY="batch-replay-1",
        )

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(first.json()["data"]["id"], replay.json()["data"]["id"])
        self.assertEqual(PayoutBatch.objects.count(), 1)
        self.assertEqual(PayoutInstruction.objects.count(), 1)

    def test_creator_without_permission_is_refused(self):
        from core.test_utils import TenantAPIClient

        user = self.User.objects.create_user(
            email="no-payout-create@test.com", password="pw", status="ACTIVE",
            first_name="No", last_name="Grant", tenant=self.tenant,
        )
        client = TenantAPIClient(user=user)
        response = client.post(
            f"/v1/payments/payouts/?entity={self.entity.code}",
            {"amount": 10_000, "vendor": self.vendor.pk},
            format="json", HTTP_IDEMPOTENCY_KEY="no-create-permission",
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_batch_detail_strips_beneficiary_bank_fields_without_sensitive_grant(self):
        from core.test_utils import TenantAPIClient
        from vs_rbac.models import Permission, TenantRolePermission

        viewer = self.User.objects.create_user(
            email="payout-view-only@test.com", password="pw", status="ACTIVE",
            first_name="View", last_name="Only", tenant=self.entity.tenant,
        )
        role = self.TenantRoleTemplate.objects.create(
            tenant=self.entity.tenant, key="payout-view-only",
            name="Payout view only", status="ACTIVE",
        )
        TenantRolePermission.objects.create(
            role=role, permission=Permission.objects.get(key="payments.payout.view"),
        )
        self.TenantUserRoleAssignment.objects.create(
            tenant=self.entity.tenant, user=viewer, role=role,
            assignment_status="ACTIVE",
        )
        batch = self._draft_batch(10_000)

        response = TenantAPIClient(user=viewer).get(
            f"/v1/payments/payout-batches/{batch.pk}/?entity={self.entity.code}",
        )

        self.assertEqual(response.status_code, 200, response.content)
        instruction = response.json()["data"]["instructions"][0]
        protected = {
            "beneficiary_name", "beneficiary_account_number", "beneficiary_bank_code",
        }
        self.assertTrue(protected.isdisjoint(instruction))
        self.assertEqual(set(instruction["_stripped_fields"]), protected)

    def test_vendor_from_another_tenant_is_rejected(self):
        from vs_tenants.models import Tenant

        other_tenant = Tenant.objects.create(
            name="Maple", slug="maple-payout-vendor",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        other_entity = LedgerEntity.objects.create(
            name="Maple Books", code="MPLPB", kind=LedgerEntity.Kind.TENANT,
            tenant=other_tenant,
        )
        other_vendor = Vendor.objects.create(
            entity=other_entity, code="OTHER", name="Other Supplier",
            bank_name="Other Bank", bank_code="999",
            bank_account_name="Other Supplier", bank_account_number="1111111111",
            kyc_status="VERIFIED",
        )

        response = self._post_single("cross-tenant-vendor", vendor=other_vendor.pk)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_single_payout_replay_returns_the_same_batch_instruction_and_workflow(self):
        from unittest.mock import patch
        from vs_workflow.models import WorkflowInstance

        self._publish_template()
        self._make_approver()
        with patch.object(
            self.fake, "create_transfer", wraps=self.fake.create_transfer,
        ) as create_transfer:
            first = self._post_single("single-replay-1")
            replay = self._post_single("single-replay-1")

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(first.json()["data"]["id"], replay.json()["data"]["id"])
        self.assertEqual(
            first.json()["data"]["batch_id"], replay.json()["data"]["batch_id"],
        )
        self.assertEqual(PayoutBatch.objects.count(), 1)
        self.assertEqual(PayoutInstruction.objects.count(), 1)
        self.assertEqual(WorkflowInstance.all_objects.count(), 1)
        instruction = PayoutInstruction.objects.get()
        self.assertEqual(instruction.beneficiary_name, self.vendor.bank_account_name)
        self.assertEqual(
            instruction.beneficiary_account_number, self.vendor.bank_account_number,
        )
        self.assertEqual(instruction.beneficiary_bank_code, self.vendor.bank_code)
        create_transfer.assert_not_called()

    def test_reusing_payout_key_for_changed_payload_returns_typed_conflict(self):
        self._publish_template()
        self._make_approver()
        self.assertEqual(self._post_single("single-conflict-1").status_code, 201)

        conflict = self._post_single("single-conflict-1", amount=20_000)

        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(
            conflict.json()["error"]["code"], "PAYOUT_IDEMPOTENCY_CONFLICT",
        )
        self.assertEqual(PayoutBatch.objects.count(), 1)

    def test_missing_template_fails_closed_without_persisting_a_single_payout(self):
        response = self._post_single("single-no-template")

        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response.json()["error"]["code"], "TEMPLATE_NOT_FOUND")
        self.assertFalse(PayoutBatch.objects.exists())
        self.assertFalse(PayoutInstruction.objects.exists())

    def test_batch_submit_flag_fails_closed_without_template(self):
        response = self.client.post(
            f"/v1/payments/payout-batches/?entity={self.entity.code}",
            {
                "submit": True,
                "items": [{"amount": 10_000, "vendor": self.vendor.pk}],
            },
            format="json", HTTP_IDEMPOTENCY_KEY="batch-no-template",
        )

        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response.json()["error"]["code"], "TEMPLATE_NOT_FOUND")
        self.assertFalse(PayoutBatch.objects.exists())
        self.assertFalse(PayoutInstruction.objects.exists())

    def test_vendor_must_be_currently_eligible_and_have_complete_bank_master(self):
        self._publish_template()
        self._make_approver()
        baseline = {
            "is_active": True, "kyc_status": "VERIFIED", "on_hold": False,
            "bank_account_name": "Supplier Ltd", "bank_account_number": "0123456789",
            "bank_code": "058",
        }
        cases = (
            ("inactive", {"is_active": False}),
            ("pending-kyc", {"kyc_status": "PENDING"}),
            ("on-hold", {"on_hold": True}),
            ("missing-account-name", {"bank_account_name": ""}),
            ("missing-account-number", {"bank_account_number": ""}),
            ("missing-bank-code", {"bank_code": ""}),
        )
        for index, (label, change) in enumerate(cases):
            with self.subTest(label=label):
                vendor_values = {**baseline, **change}
                Vendor.objects.filter(pk=self.vendor.pk).update(**vendor_values)
                response = self._post_single(f"vendor-gate-{index}")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_legacy_beneficiary_value_must_match_vendor_master(self):
        self._publish_template()
        self._make_approver()

        response = self._post_single(
            "single-mismatch", beneficiary_account_number="9999999999",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_bank_change_after_submission_blocks_provider_dispatch(self):
        from unittest.mock import patch
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.services import actions as wf_actions

        self._publish_template()
        approver = self._make_approver()
        response = self._post_single("single-bank-change")
        batch = PayoutBatch.objects.get(pk=response.json()["data"]["batch_id"])
        # Bypass the model reset deliberately to prove the exact snapshot check is
        # independent of KYC reset and runs again at the provider boundary.
        Vendor.objects.filter(pk=self.vendor.pk).update(
            bank_account_number="9999999999", kyc_status="VERIFIED",
        )

        with patch.object(
            self.fake, "create_transfer", wraps=self.fake.create_transfer,
        ) as create_transfer:
            with self.assertRaises(PaymentStateError):
                wf_actions.record_action(
                    self._instance_for(batch).id, approver, ActionEnum.APPROVED,
                )

        create_transfer.assert_not_called()
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)

    # --- 1. direct provider submission is always closed -------------------- #

    def test_direct_submit_without_template_is_refused(self):
        batch = self._draft_batch(10000, 20000)
        resp = self._direct_submit(batch)
        self.assertEqual(resp.status_code, 409, resp.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)

    # --- 2. gate on: direct submit refused --------------------------------- #

    def test_gate_on_direct_submit_refused(self):
        self._publish_template()
        batch = self._draft_batch(10000)
        resp = self._direct_submit(batch)
        self.assertEqual(resp.status_code, 409, resp.content)
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

    # --- 3b. another tenant cannot submit this batch ----------------------- #

    def test_another_tenant_cannot_submit_this_batch(self):
        """The reported hole, on a real batch.

        A finance user at a different tenant used to be able to POST this batch's pk
        to the generic ``/v1/workflow/instances/`` endpoint. The instance was
        created inside *this* tenant, the handler marked the batch pending
        approval, and the 201 handed the outsider the reference, the total, the
        provider and the names of this tenant's approvers. The endpoint is gone;
        this asserts the service refuses even when a caller reaches it directly.
        """
        from vs_tenants.models import Tenant
        from vs_workflow.exceptions import CrossTenantDocumentError
        from vs_workflow.models import WorkflowInstance
        from vs_workflow.services.submission import submit_for_approval

        self._publish_template()
        self._make_approver()
        batch = self._draft_batch(386_000_000)

        outsider_tenant = Tenant.objects.create(
            name="Greenfield", slug="greenfield-pba",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        outsider = self.User.objects.create_user(
            email="bursar-greenfield@test.com", password="pw", status="ACTIVE",
            first_name="Tunde", last_name="Bursar", tenant=outsider_tenant,
        )

        with self.assertRaises(CrossTenantDocumentError):
            submit_for_approval(document=batch, requested_by=outsider)

        # No instance filed into Cedar, and the batch is untouched: still DRAFT,
        # no approval_status stamped, every instruction still pending.
        self.assertFalse(WorkflowInstance.objects.for_document(batch).exists())
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        self.assertIsNone((batch.metadata or {}).get("approval_status"))
        self.assertTrue(all(p.status == PayoutStatus.PENDING
                            for p in batch.instructions.all()))

    def test_the_owning_tenant_still_submits_the_same_batch(self):
        """The control for the test above: the guard refuses outsiders only."""
        from vs_workflow.services.submission import submit_for_approval

        self._publish_template()
        self._make_approver()
        batch = self._draft_batch(386_000_000)

        instance = submit_for_approval(document=batch, requested_by=self.requester)

        self.assertEqual(instance.tenant, self.tenant)
        batch.refresh_from_db()
        self.assertEqual((batch.metadata or {}).get("approval_status"), "PENDING_APPROVAL")

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

    def test_an_approved_instance_cannot_dispatch_a_different_batch(self):
        from unittest.mock import patch
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.services import actions as wf_actions

        self._publish_template()
        approver = self._make_approver()
        approved_batch = self._draft_batch(10_000)
        target_batch = self._draft_batch(20_000)
        self._submit_for_approval(approved_batch)
        self._submit_for_approval(target_batch)
        approved_instance = self._instance_for(approved_batch)
        wf_actions.record_action(approved_instance.id, approver, ActionEnum.APPROVED)

        with patch.object(
            self.fake, "create_transfer", wraps=self.fake.create_transfer,
        ) as create_transfer:
            with self.assertRaises(PaymentStateError):
                services.submit_payout_batch(
                    target_batch, approved_instance=approved_instance,
                )

        create_transfer.assert_not_called()
        self.assertTrue(target_batch.instructions.filter(status=PayoutStatus.PENDING).exists())

    def test_locked_batch_integrity_is_rechecked_before_provider_dispatch(self):
        from unittest.mock import patch
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.services import actions as wf_actions

        self._publish_template()
        approver = self._make_approver()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)
        batch.instructions.update(
            status=PayoutStatus.PENDING, provider_reference="", recipient_code="",
        )
        PayoutBatch.objects.filter(pk=batch.pk).update(item_count=2)

        with patch.object(
            self.fake, "create_transfer", wraps=self.fake.create_transfer,
        ) as create_transfer:
            with self.assertRaises(PaymentStateError):
                services.submit_payout_batch(batch, approved_instance=instance)

        create_transfer.assert_not_called()

    # --- 7. the seeded ladder, not a hand-made one ------------------------- #

    def _seed_tenant_ladder(self, **kwargs):
        """Publish the real shipped ladder for this tenant."""
        from vs_payments.approvals import ensure_tenant_approval_templates

        self._ensure_approve_role()  # Publishing names it; nobody is in it yet.
        template, _created = ensure_tenant_approval_templates(
            self.tenant, **kwargs)
        return template

    def _grant(self, user, permission_key, role_key):
        role, _ = self.TenantRoleTemplate.objects.get_or_create(
            tenant=self.tenant, key=role_key,
            defaults={"name": role_key, "status": "ACTIVE"},
        )
        self.TenantRolePermission.objects.get_or_create(
            role=role, permission_id=permission_key, defaults={"granted": True},
        )
        self.TenantUserRoleAssignment.objects.get_or_create(
            tenant=self.tenant, user=user, role=role,
            defaults={"assignment_status": "ACTIVE"},
        )

    def test_the_seeded_ladder_makes_the_approval_route_available(self):
        """The real seed creates an applicable route without weakening direct submit."""
        from vs_finance.approvals import approval_required

        batch = self._draft_batch(10000)
        self.assertFalse(approval_required(batch))  # No workflow route before seeding.
        self._seed_tenant_ladder()
        batch.refresh_from_db()
        self.assertTrue(approval_required(batch))  # The approval route now applies.
        resp = self._direct_submit(batch)
        self.assertEqual(resp.status_code, 409, resp.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        self.assertTrue(all(p.status == PayoutStatus.PENDING for p in batch.instructions.all()))

    def test_a_seeded_batch_with_nobody_appointed_parks_instead_of_paying(self):
        """Seeded blocked, not seeded open: no approver means no money moves."""
        from vs_workflow.constants import WorkflowInstanceStatus

        self._seed_tenant_ladder()  # Nobody holds the approving permission yet.
        batch = self._draft_batch(10000)
        resp = self._submit_for_approval(batch)
        self.assertEqual(resp.status_code, 200, resp.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)  # Not dispatched.
        self.assertEqual((batch.metadata or {}).get("approval_status"), "PENDING_APPROVAL")
        self.assertEqual(self._instance_for(batch).status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertTrue(all(p.status == PayoutStatus.PENDING for p in batch.instructions.all()))

    def test_one_approval_releases_the_batch(self):
        """A low-value batch skips the senior stage after one checker vote."""
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._seed_tenant_ladder()
        approver = self._make_approver()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)

        wf_actions.record_action(self._instance_for(batch).id, approver, ActionEnum.APPROVED)

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)

    def test_high_value_batch_needs_a_distinct_senior_approver(self):
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        self._seed_tenant_ladder()
        approver = self._make_approver()
        senior = self._make_senior_approver()
        batch = self._draft_batch(90_000_000, 90_000_000)
        self._submit_for_approval(batch)

        wf_actions.record_action(self._instance_for(batch).id, approver, ActionEnum.APPROVED)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)

        wf_actions.record_action(self._instance_for(batch).id, senior, ActionEnum.APPROVED)

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)

    def test_same_person_cannot_satisfy_both_high_value_stages(self):
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.services import actions as wf_actions

        self._seed_tenant_ladder()
        approver = self._make_approver()
        high_role = self.TenantRoleTemplate.objects.get(
            tenant=self.tenant, key="payout-senior-approver",
        )
        self.TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=approver, role=high_role,
            assignment_status="ACTIVE",
        )
        batch = self._draft_batch(50_000_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)
        with self.assertRaises(PaymentStateError):
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)

    # --- 10. surviving a change in how approvers are resolved -------------- #

    def test_an_unknown_approver_source_is_repaired_not_skipped(self):
        """A source the repair has not been taught about must resolve live.

        The approver model is being reworked away from permission keys. If the
        resolution cache treated an unrecognised source as "provably nobody", every
        stage using the new model would park and never un-park, which is the exact
        defect this repair exists to prevent. Costing a query is the acceptable
        failure here; losing a document is not.
        """
        from vs_workflow.services.parking import ResolutionCache

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage_instance = instance.stage_instances.filter(status="ACTIVE").get()
        stage = stage_instance.stage

        # Stand in for an approver source this code has never been taught.
        stage.approver_source = "GROUP_MEMBERSHIP"
        stage.approver_role_key = ""
        stage.save(update_fields=["approver_source", "approver_role_key"])

        cache = ResolutionCache()
        self.assertTrue(
            cache.has_candidates(stage, instance),
            "an unrecognised approver source was written off without resolving it",
        )

    def test_a_role_stage_with_no_key_is_not_written_off(self):
        """Misconfigured is not the same as unstaffable; let the resolver decide.

        Both the portable key and the resolved FK have to go: ``stage_role_key``
        prefers the key and falls back to the relation, so clearing one alone
        still yields a key and the memo can legitimately answer "nobody".
        """
        from vs_workflow.services.parking import ResolutionCache

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage = instance.stage_instances.filter(status="ACTIVE").get().stage
        stage.approver_role_key = ""
        stage.approver_role = None
        stage.save(update_fields=["approver_role_key", "approver_role"])

        self.assertTrue(ResolutionCache().has_candidates(stage, instance))

    def test_the_warning_always_says_how_to_fix_it(self):
        """`requirement` is what the dialog renders, so it can never come back blank."""
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage = instance.stage_instances.filter(status="ACTIVE").get().stage

        park = release.describe_park(instance)
        self.assertEqual(park["approver_source"], "ROLE")
        self.assertIn(self.APPROVE_ROLE, park["requirement"])

        # An approver model this code has never seen still yields an instruction.
        stage.approver_source = "GROUP_MEMBERSHIP"
        stage.approver_role_key = ""
        stage.save(update_fields=["approver_source", "approver_role_key"])
        park = release.describe_park(instance)
        self.assertTrue(park["parked"])
        self.assertTrue(park["requirement"].strip(), "the dialog would render a blank")
        # A key that no longer decides anything must not be shown as if it does.
        self.assertEqual(park["role_key"], "")

    def test_an_organogram_stage_explains_itself_in_org_terms(self):
        """Not every stage is fixed by granting a permission."""
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage = instance.stage_instances.filter(status="ACTIVE").get().stage
        stage.approver_source = "ORGANOGRAM"
        stage.organogram_target = "DEPARTMENT_HEAD"
        stage.save(update_fields=["approver_source", "organogram_target"])

        park = release.describe_park(instance)
        self.assertIn("department", park["requirement"])
        self.assertNotIn("role", park["requirement"])
        self.assertEqual(park["role_key"], "")

    def test_an_unknown_source_cannot_silently_skip_its_own_approval(self):
        """The dangerous half: a broken template must not approve spend by itself.

        `skip_if_no_approvers` defaults to True, and routing skips a stage that
        resolves to nobody. So a resolver that answered "no approvers" for a source
        it did not recognise would let a configuration mistake wave a payout through
        unreviewed. An unrecognised source has to raise instead.
        """
        from vs_workflow.exceptions import UnknownApproverSourceError
        from vs_workflow.services import approvers as approvers_service

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage = instance.stage_instances.filter(status="ACTIVE").get().stage
        stage.approver_source = "GROUP_MEMBERSHIP"
        stage.skip_if_no_approvers = True  # The model default, and the risky one.
        stage.save(update_fields=["approver_source", "skip_if_no_approvers"])

        with self.assertRaises(UnknownApproverSourceError):
            approvers_service.resolve_approvers(stage, instance)

    def test_one_broken_template_does_not_break_everyone_else_s_inbox(self):
        """The resolver raises by design; the read path must survive it.

        The repair runs on every approvals-queue read, over whatever is parked in
        the tenant. If an unresolvable stage propagated out of it, one team's
        misconfigured template would take the approvals inbox down for everybody.
        """
        import logging
        from vs_workflow.services import my_queue, parking

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage = instance.stage_instances.filter(status="ACTIVE").get().stage
        stage.approver_source = "GROUP_MEMBERSHIP"
        stage.save(update_fields=["approver_source"])

        logging.disable(logging.CRITICAL)  # The sweep logs the traceback by design.
        self.addCleanup(logging.disable, logging.NOTSET)

        # Both the sweep and the queue read it powers stay standing.
        self.assertEqual(parking.repair_workflows(tenant=self.tenant), 0)
        self.assertEqual(
            my_queue.pending_approval_snapshots(self.requester, self.tenant), [])

    def test_a_role_stage_with_no_key_still_resolves_to_nobody(self):
        """Not every empty answer is a misconfiguration; this one is legitimate."""
        from vs_workflow.services import approvers as approvers_service

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        stage = instance.stage_instances.filter(status="ACTIVE").get().stage
        stage.approver_role_key = ""
        stage.save(update_fields=["approver_role_key"])

        self.assertEqual(approvers_service.resolve_approvers(stage, instance), [])

    # --- 9. continuing when nobody can approve ----------------------------- #

    def test_a_submitter_is_told_when_nobody_can_approve(self):
        """The warning's data: the submit response says it parked, and on what."""
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)

        park = release.approval_block(self._instance_for(batch))
        self.assertTrue(park["parked"])
        self.assertEqual(park["stage_label"], "Payout checker approval")
        self.assertFalse(park["can_continue_without_approval"])
        # Names the role an administrator would fill, rather than just "no approver".
        self.assertEqual(park["role_key"], self.APPROVE_ROLE)

    def test_continuing_without_approval_is_refused_for_payouts(self):
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        with self.assertRaises(release.ReleaseNotAllowedError):
            release.release_parked_stage(
                instance, actor_user=self.requester, reason="Payroll is due.",
            )

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)
        instance.refresh_from_db()
        self.assertEqual(instance.status, "IN_PROGRESS")

    def test_continuing_is_refused_while_anybody_can_still_approve(self):
        """The safety property: this is never a bypass of a reviewer who exists.

        Without it the dialog would be a self-approval button on any document whose
        approver simply had not got to it yet.
        """
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        self._make_approver()  # Somebody can decide it.
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)

        with self.assertRaises(release.ReleaseNotAllowedError):
            release.release_parked_stage(
                self._instance_for(batch), actor_user=self.requester, reason="Let me through.")

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)  # Nothing dispatched.

    def test_a_late_granted_approver_cancels_the_bypass(self):
        """Between the warning and the click, somebody was appointed. Review wins."""
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)
        self.assertTrue(release.describe_park(instance)["parked"])  # Warning was right.

        self._make_approver()  # Appointed while the dialog was open.

        with self.assertRaises(release.ReleaseNotAllowedError):
            release.release_parked_stage(instance, actor_user=self.requester)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.DRAFT)

    def test_refused_release_writes_no_override_record(self):
        from vs_workflow.models import WorkflowAuditLog
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        with self.assertRaises(release.ReleaseNotAllowedError):
            release.release_parked_stage(
                instance, actor_user=self.requester, reason="Payroll is due today.")

        self.assertFalse(WorkflowAuditLog.objects.filter(
            instance=instance, context__action="RELEASED_NO_APPROVER").exists())

    def test_release_endpoint_reports_the_domain_policy(self):
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        response = self.client.post(
            f"/v1/workflow/instances/{instance.pk}/continue-without-approval/",
            {}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(
            response.json()["error"]["code"],
            "CONTINUE_WITHOUT_APPROVAL_NOT_ALLOWED",
        )

    def test_only_the_submitter_may_continue(self):
        """Ungated by permission is not the same as open to every user in the tenant."""
        from vs_workflow.services import release

        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        instance = self._instance_for(batch)

        stranger = self.User.objects.create_user(
            email="stranger-pba@test.com", password="pw", status="ACTIVE", first_name="Stran", last_name="Ger",
            tenant=self.tenant,
        )
        self.assertFalse(release.may_release(instance, stranger))
        self.assertTrue(release.may_release(instance, instance.requested_by))
        self.assertFalse(release.handler_allows_release(instance))

    # --- 8. parked work becomes reachable again ---------------------------- #

    def _park_a_batch(self):
        """Submit under a seeded ladder with nobody appointed. Returns the batch."""
        self._seed_tenant_ladder()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        return batch

    def test_a_late_appointed_approver_is_invisible_until_the_repair_runs(self):
        """The trap itself: the snapshot froze empty, so the grant alone changes nothing."""
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.exceptions import NotAnEligibleApproverError

        batch = self._park_a_batch()
        approver = self._make_approver()  # Granted only now, after activation.
        with self.assertRaises(NotAnEligibleApproverError):
            wf_actions.record_action(
                self._instance_for(batch).id, approver, ActionEnum.APPROVED)

    def test_the_repair_releases_a_parked_batch_without_resubmitting(self):
        """A payout batch gets the repair procurement always had.

        Before this was moved into the engine the scan was fenced to procurement's
        four document types, so a parked payout batch stayed stuck for that attempt
        and the only recovery was withdraw and resubmit.
        """
        from vs_workflow.services import actions as wf_actions
        from vs_workflow.services import my_queue
        from vs_workflow.constants import WorkflowStageAction as ActionEnum

        batch = self._park_a_batch()
        outsider = self.User.objects.create_user(
            email="nobody-pba@test.com", password="pw", status="ACTIVE", first_name="No", last_name="Body",
            tenant=self.tenant,
        )
        # Nobody holds the key yet, so there is nothing to restore and nothing to see.
        self.assertEqual(my_queue.pending_approval_snapshots(outsider, self.tenant), [])

        approver = self._make_approver()  # Appointed after the stage already activated.
        # Reading the queue is what repairs it; no resubmission anywhere.
        snaps = my_queue.pending_approval_snapshots(approver, self.tenant)
        self.assertEqual(len(snaps), 1)

        wf_actions.record_action(self._instance_for(batch).id, approver, ActionEnum.APPROVED)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)

    def test_the_repair_leaves_a_staffed_stage_alone(self):
        """The freeze guarantee: a populated snapshot is never rewritten or added to."""
        from vs_workflow.models import WorkflowStageApprover
        from vs_workflow.services import parking

        self._seed_tenant_ladder()
        first = self._make_approver()
        batch = self._draft_batch(10_000)
        self._submit_for_approval(batch)
        before = set(WorkflowStageApprover.objects.values_list("pk", flat=True))
        self.assertTrue(before)

        # A second holder appears after activation. The stage is staffed, so the
        # repair must not reach in and add them mid-review.
        self._make_approver(email="apr2-pba@test.com")
        self.assertEqual(parking.repair_workflows(tenant=self.tenant), 0)
        self.assertEqual(
            set(WorkflowStageApprover.objects.values_list("pk", flat=True)), before)
        self.assertTrue(WorkflowStageApprover.objects.filter(user=first).exists())

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


# Group tests for the seeded payout-approval ladder (turning the gate ON).
class PayoutApprovalSeedingTests(TestCase):
    """Provisioning the default payout approval policy.

    The provider boundary is mandatory independently of the template. These tests pin
    the default two-stage ladder and its non-destructive provisioning behavior.
    """

    APPROVE_ROLE = "payout-approver"

    def setUp(self):
        import io
        from django.core.management import call_command
        from vs_tenants.models import Tenant

        call_command("seed_payments_permissions", verbosity=0, stdout=io.StringIO())
        self.tenant = Tenant.objects.create(
            name="Rowan", slug="rowan-seed", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )

    def _stages(self, template):
        """The template's live stages, in routing order."""
        return list(template.stages.filter(retired_at__isnull=True).order_by("order"))

    # --- shape ------------------------------------------------------------- #

    def test_the_platform_fallback_publishes_the_two_stage_ladder(self):
        from vs_payments.approvals import ensure_default_approval_templates

        template = ensure_default_approval_templates()
        self.assertIsNone(template.tenant)  # Platform-scoped fallback.
        self.assertIsNone(template.branch)
        checker, senior = self._stages(template)
        self.assertEqual(checker.approver_role_key, self.APPROVE_ROLE)
        self.assertEqual(checker.approver_source, "ROLE")
        self.assertIsNone(checker.inclusion_condition)  # Always runs.
        self.assertEqual(checker.advance_rule, "ANY")
        self.assertEqual(senior.approver_role_key, "payout-senior-approver")
        self.assertEqual(
            senior.inclusion_condition,
            {"op": "gte", "field": "total_amount", "value": 50_000_000},
        )

    def test_no_stage_may_ever_auto_skip_itself(self):
        """The whole point: an unstaffed stage must park, not approve.

        The engine's model default is ``skip_if_no_approvers=True``, so a ladder seeded
        without overriding it would sail a batch through while *appearing* approved,
        which is worse than no gate at all.
        """
        from vs_payments.approvals import ensure_default_approval_templates

        stages = self._stages(ensure_default_approval_templates())
        self.assertTrue(stages)
        for stage in stages:
            self.assertFalse(stage.skip_if_no_approvers, stage.code)
            self.assertEqual(stage.on_rejection, "TERMINAL", stage.code)

    def test_the_approving_role_is_configurable(self):
        from vs_payments.approvals import ensure_default_approval_templates

        template = ensure_default_approval_templates(approve_role_key="other-approver")
        checker, _senior = self._stages(template)
        self.assertEqual(checker.approver_role_key, "other-approver")

    # --- provisioning semantics -------------------------------------------- #

    def test_a_tenant_gets_its_own_ladder(self):
        from vs_payments.approvals import ensure_tenant_approval_templates

        template, created = ensure_tenant_approval_templates(self.tenant)
        self.assertTrue(created)
        self.assertEqual(template.tenant_id, self.tenant.pk)
        self.assertEqual(len(self._stages(template)), 2)
        self.assertTrue(self.tenant.role_templates.filter(
            key="payout-senior-approver",
        ).exists())

    def test_reseeding_a_tenant_never_overwrites_what_an_admin_configured(self):
        """Non-destructive by contract: a customised ladder survives a re-run."""
        from vs_payments.approvals import ensure_tenant_approval_templates

        template, _ = ensure_tenant_approval_templates(
            self.tenant, approve_role_key="other-approver")
        again, created = ensure_tenant_approval_templates(self.tenant)
        self.assertFalse(created)
        self.assertEqual(again.pk, template.pk)
        checker, _senior = self._stages(again)
        self.assertEqual(checker.approver_role_key, "other-approver")  # Untouched.

    def test_reseeding_the_platform_row_upserts_rather_than_duplicating(self):
        from vs_workflow.models import WorkflowTemplate
        from vs_payments.approvals import ensure_default_approval_templates

        ensure_default_approval_templates()
        ensure_default_approval_templates(approve_role_key="other-approver")
        rows = WorkflowTemplate.all_objects.filter(
            tenant=None, branch=None, document_type="payments.payout_batch")
        self.assertEqual(rows.count(), 1)  # One shared row, rewritten in place.
        checker, _senior = self._stages(rows.get())
        self.assertEqual(checker.approver_role_key, "other-approver")

    def test_a_tenant_is_required(self):
        from vs_payments.approvals import ensure_tenant_approval_templates

        with self.assertRaises(ValueError):
            ensure_tenant_approval_templates(None)

    def test_the_backfill_migration_creates_the_platform_fallback_once(self):
        """Pre-provisioning tenants gain a route without a destructive republish."""
        import importlib
        from django.apps import apps
        from vs_workflow.models import WorkflowTemplate

        WorkflowTemplate.all_objects.filter(
            document_type="payments.payout_batch",
        ).delete()
        migration = importlib.import_module(
            "vs_payments.migrations.0006_seed_platform_payout_approval_fallback",
        )
        migration.seed_platform_payout_approval_fallback(apps, None)
        migration.seed_platform_payout_approval_fallback(apps, None)

        templates = WorkflowTemplate.all_objects.filter(
            tenant=None,
            branch=None,
            document_type="payments.payout_batch",
            code="standard",
        )
        self.assertEqual(templates.count(), 1)
        template = templates.get()
        checker, senior = self._stages(template)
        self.assertEqual(checker.approver_role_key, "payout-approver")
        self.assertFalse(checker.skip_if_no_approvers)
        self.assertEqual(
            senior.inclusion_condition,
            {"op": "gte", "field": "total_amount", "value": 50_000_000},
        )

    def test_the_backfill_migration_does_not_overwrite_platform_configuration(self):
        """A platform policy that already exists remains administrator-owned."""
        import importlib
        from django.apps import apps
        from vs_payments.approvals import ensure_default_approval_templates

        template = ensure_default_approval_templates(
            approve_role_key="custom-checker",
            high_value_threshold=75_000_000,
        )
        migration = importlib.import_module(
            "vs_payments.migrations.0006_seed_platform_payout_approval_fallback",
        )
        migration.seed_platform_payout_approval_fallback(apps, None)

        template.refresh_from_db()
        checker, senior = self._stages(template)
        self.assertEqual(checker.approver_role_key, "custom-checker")
        self.assertEqual(senior.inclusion_condition["value"], 75_000_000)

    # --- the command ------------------------------------------------------- #

    def test_the_command_refuses_to_guess_what_to_seed(self):
        import io
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("seed_payout_approvals", verbosity=0, stdout=io.StringIO())

    def test_the_command_seeds_a_named_tenant(self):
        import io
        from django.core.management import call_command
        from vs_workflow.models import WorkflowTemplate

        call_command(
            "seed_payout_approvals", tenants=[self.tenant.slug],
            verbosity=0, stdout=io.StringIO())
        self.assertTrue(WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, document_type="payments.payout_batch").exists())

    def test_a_dry_run_writes_nothing(self):
        import io
        from django.core.management import call_command
        from vs_workflow.models import WorkflowTemplate

        WorkflowTemplate.all_objects.filter(
            document_type="payments.payout_batch",
        ).delete()
        call_command(
            "seed_payout_approvals", platform=True, tenants=[self.tenant.slug],
            dry_run=True, verbosity=0, stdout=io.StringIO())
        self.assertFalse(WorkflowTemplate.all_objects.filter(
            document_type="payments.payout_batch").exists())


class PayoutApprovalHealthTests(TestCase):
    """The deployment check names every active entity with no payout route."""

    def setUp(self):
        from vs_finance.models import LedgerEntity
        from vs_tenants.models import Tenant
        from vs_workflow.models import WorkflowTemplate

        WorkflowTemplate.all_objects.filter(
            document_type="payments.payout_batch",
        ).delete()

        self.tenant = Tenant.objects.create(
            name="Health Tenant",
            slug="payout-health",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        self.entity = LedgerEntity.objects.create(
            name="Health Books",
            code="PHTH",
            kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )

    def test_every_unroutable_active_entity_is_listed(self):
        from vs_finance.models import LedgerEntity
        from vs_payments.approvals import payout_approval_template_gaps

        second = LedgerEntity.objects.create(
            name="Second Health Books",
            code="PHT2",
            kind=LedgerEntity.Kind.OTHER,
            tenant=self.tenant,
        )

        gaps = payout_approval_template_gaps()

        rows = {(gap["tenant_slug"], gap["entity_code"]) for gap in gaps}
        self.assertIn((self.tenant.slug, self.entity.code), rows)
        self.assertIn((self.tenant.slug, second.code), rows)

    def test_platform_fallback_makes_every_entity_routable(self):
        from vs_payments.approvals import (
            ensure_default_approval_templates,
            payout_approval_template_gaps,
        )

        ensure_default_approval_templates()

        self.assertEqual(payout_approval_template_gaps(), [])

    def test_inactive_entities_and_tenants_are_not_release_blockers(self):
        from vs_finance.models import LedgerEntity
        from vs_payments.approvals import payout_approval_template_gaps
        from vs_tenants.models import Tenant

        self.entity.is_active = False
        self.entity.save(update_fields=["is_active"])
        inactive_tenant = Tenant.objects.create(
            name="Inactive Tenant",
            slug="inactive-payout-health",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.INACTIVE,
        )
        LedgerEntity.objects.create(
            name="Inactive Books",
            code="PIHB",
            kind=LedgerEntity.Kind.TENANT,
            tenant=inactive_tenant,
        )

        affected_codes = {
            gap["entity_code"] for gap in payout_approval_template_gaps()
        }
        self.assertNotIn("PHTH", affected_codes)
        self.assertNotIn("PIHB", affected_codes)

    def test_command_fails_with_the_affected_entity_named(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError

        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("payout_approval_health", stdout=output, stderr=output)
        report = output.getvalue()
        self.assertIn("tenant=payout-health", report)
        self.assertIn("entity=PHTH", report)

    def test_command_passes_after_the_fallback_is_published(self):
        from io import StringIO
        from django.core.management import call_command
        from vs_payments.approvals import ensure_default_approval_templates

        ensure_default_approval_templates()
        output = StringIO()

        call_command("payout_approval_health", stdout=output, stderr=output)

        self.assertIn("Healthy", output.getvalue())


class PayoutApprovalDataMigrationTests(TestCase):
    """The ladder upgrade recognizes shipped data and leaves custom policy alone."""

    def _template(self, tenant, *, stage_code="approver"):
        from vs_rbac.models import TenantRoleTemplate
        from vs_workflow.models import WorkflowStage, WorkflowTemplate

        role = TenantRoleTemplate.objects.create(
            tenant=tenant, key="payout-approver", name="Payout Approver",
            status="ACTIVE",
        )
        template = WorkflowTemplate.all_objects.create(
            tenant=tenant, branch=None, document_type="payments.payout_batch",
            code="standard", name="Payout-batch approval",
            description="Approval rule for a payout batch.",
        )
        WorkflowStage.objects.create(
            template=template, code=stage_code, label="Payout approval",
            kind="APPROVAL", order=10, approver_source="ROLE",
            approver_scope="SCHOOL", approver_role_key="payout-approver",
            approver_role=role, advance_rule="ANY", quorum_count=0,
            on_rejection="TERMINAL", skip_if_no_approvers=False,
            inclusion_condition=None,
        )
        return template

    def test_only_exact_seeded_one_stage_template_is_upgraded(self):
        import importlib
        from django.apps import apps as django_apps
        from vs_rbac.models import TenantRoleTemplate
        from vs_tenants.models import Tenant

        seeded_tenant = Tenant.objects.create(
            name="Seeded", slug="seeded-payout-migration",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        custom_tenant = Tenant.objects.create(
            name="Custom", slug="custom-payout-migration",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        seeded = self._template(seeded_tenant)
        custom = self._template(custom_tenant, stage_code="custom-check")
        migration = importlib.import_module(
            "vs_payments.migrations.0005_upgrade_seeded_payout_ladders",
        )

        migration.upgrade_seeded_ladders(django_apps, None)

        self.assertEqual(
            list(seeded.stages.order_by("order").values_list("code", flat=True)),
            ["approver", "senior"],
        )
        self.assertTrue(TenantRoleTemplate.objects.filter(
            tenant=seeded_tenant, key="payout-senior-approver",
        ).exists())
        self.assertEqual(
            list(custom.stages.values_list("code", flat=True)), ["custom-check"],
        )
        self.assertFalse(TenantRoleTemplate.objects.filter(
            tenant=custom_tenant, key="payout-senior-approver",
        ).exists())

# Group tests for Paystack Adapter Tests (real adapter, network function mocked).
class PaystackAdapterTests(TestCase):
    """Drive the real :class:`PaystackProvider` with recorded PSP payloads.

    Every network hop goes through ``vs_payments.providers.http.request_json``, imported
    into the paystack module as ``request_json`` - so we patch it *there* (at the point of
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

    # Verify a dedicated-account deposit names its destination NUBAN behavior.
    def test_parse_webhook_extracts_the_dedicated_account_number(self):
        """Paystack reports a virtual-account transfer as a plain ``charge.success``.

        The receiving NUBAN is the only thing tying that event to a payer we know, and
        it is nested in the authorization block (repeated in metadata on some payloads),
        so the parser has to dig it out. An ordinary card charge must stay empty or a
        normal collection would be mistaken for a deposit.
        """
        deposit = {"event": "charge.success", "data": {
            "reference": "R2", "status": "success", "amount": 40000, "id": 100,
            "channel": "dedicated_nuban",
            "authorization": {"channel": "dedicated_nuban",
                              "receiver_bank_account_number": "9988776655"}}}
        parsed = self.provider.parse_webhook(payload=deposit, raw_body=b"", headers={})
        self.assertEqual(parsed.destination_account_number, "9988776655")

        from_metadata = {"event": "charge.success", "data": {
            "reference": "R3", "status": "success", "amount": 40000, "id": 101,
            "metadata": {"receiver_account_number": "1122334455"}}}
        parsed = self.provider.parse_webhook(payload=from_metadata, raw_body=b"", headers={})
        self.assertEqual(parsed.destination_account_number, "1122334455")

        # Paystack allows metadata to arrive as a bare string, and a card charge has no
        # receiver at all; neither may be read as a deposit.
        card = {"event": "charge.success", "data": {
            "reference": "R4", "status": "success", "amount": 40000, "id": 102,
            "metadata": "custom text", "authorization": {"channel": "card"}}}
        parsed = self.provider.parse_webhook(payload=card, raw_body=b"", headers={})
        self.assertEqual(parsed.destination_account_number, "")

        transfer = {"event": "transfer.success", "data": {
            "reference": "P2", "status": "success", "amount": 15000,
            "authorization": {"receiver_bank_account_number": "9988776655"}}}
        parsed = self.provider.parse_webhook(payload=transfer, raw_body=b"", headers={})
        self.assertEqual(parsed.destination_account_number, "")  # Payouts never carry one.


# Group tests for Webhook Provider Resolution.
class WebhookProviderResolutionTests(TestCase):
    """Guard the webhook receiver's provider lookup."""

    # Verify an unknown provider is rejected before any processing behavior.
    def test_unknown_provider_raises_not_configured(self):
        with self.assertRaises(ProviderNotConfiguredError):
            webhooks.ingest_webhook(provider="nope", raw_body=b"{}", headers={})


class FailedWebhookVisibilityTests(_PaymentsFixtureMixin, TestCase):
    """Money that arrives but cannot be booked has to be findable.

    ``process_stored_event`` marks a failed dispatch FAILED and deliberately swallows
    the exception - right for the PSP, which has already been acked and whose retries
    are idempotent. But nothing surfaced the result: there was no endpoint listing
    WebhookEvent at all, so a customer's payment could arrive, fail to book, and exist
    only as a database row nobody would look at.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from core.test_utils import TenantAPIClient

        from django.core.management import call_command
        call_command("seed_actions", verbosity=0)
        call_command("seed_payments_permissions", verbosity=0)

        self.User = get_user_model()
        self.user = self.User.objects.create_user(tenant=_platform_tenant(), 
            email="webhook-admin@test.com", password="testpass123",
            status="ACTIVE", first_name="Hook", last_name="Admin",
        )
        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=Tenant.objects.get(slug="codex"), key="xvs_super_admin",
            defaults={"name": "Super Admin", "status": "ACTIVE"})
        TenantUserRoleAssignment.objects.create(
            tenant=Tenant.objects.get(slug="codex"), user=self.user, role=role,
            assignment_status="ACTIVE")
        self.client = TenantAPIClient(user=self.user)

    def _unprivileged_client(self):
        """A live user holding no payments keys at all."""
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from core.test_utils import TenantAPIClient

        user = self.User.objects.create_user(tenant=_platform_tenant(), 
            email="webhook-nobody@test.com", password="testpass123",
            status="ACTIVE", first_name="No", last_name="Rights",
        )
        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=Tenant.objects.get(slug="codex"), key="xvs_no_rights",
            defaults={"name": "No Rights", "status": "ACTIVE"})
        TenantUserRoleAssignment.objects.create(
            tenant=Tenant.objects.get(slug="codex"), user=user, role=role,
            assignment_status="ACTIVE")
        return TenantAPIClient(user=user)

    def _close_all_periods(self, entity):
        """Leave the entity with no open period, so a receipt cannot post today."""
        FiscalPeriod.objects.filter(entity=entity).update(status="CLOSED")

    def _failed_collection_webhook(self, entity, customer):
        """Drive a real collection webhook while nothing can post, and return the event."""
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, narration="Fees")
        # The dispatcher deliberately does not trust the status carried by the webhook;
        # it re-polls the provider before moving money, so the fake has to agree.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        self._close_all_periods(entity)
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=intent.reference,
            status="SUCCEEDED", amount=50000)
        event = webhooks.ingest_webhook(
            provider="FAKE", raw_body=raw, headers=headers)
        webhooks.process_stored_event(event.id)
        event.refresh_from_db()
        return intent, event

    def test_a_failed_booking_is_recorded_and_books_nothing(self):
        entity, customer, _vendor = self.build()
        intent, event = self._failed_collection_webhook(entity, customer)

        self.assertEqual(event.status, "FAILED")
        self.assertTrue(event.error)  # the reason is kept for the operator
        self.assertFalse(Payment.objects.filter(entity=entity).exists())
        intent.refresh_from_db()
        self.assertIsNone(intent.payment_id)
        # The event stays attributable even though the confirm blew up - without this
        # it belongs to no entity and no entity-scoped screen can ever show it.
        self.assertEqual(event.collection_id, intent.pk)

    def test_the_failed_event_is_listed_for_its_entity(self):
        entity, customer, _vendor = self.build()
        _intent, event = self._failed_collection_webhook(entity, customer)

        resp = self.client.get(f"/v1/payments/webhooks/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertEqual([r["id"] for r in rows], [event.id])
        self.assertEqual(rows[0]["status"], "FAILED")
        self.assertEqual(rows[0]["target_kind"], "COLLECTION")
        self.assertEqual(rows[0]["amount"], 50000)
        self.assertTrue(rows[0]["error"])
        # The provider's raw record is never handed to the console.
        for leaked in ("payload", "raw_body", "headers", "signature"):
            self.assertNotIn(leaked, rows[0])

    def test_the_summary_counts_what_needs_attention(self):
        entity, customer, _vendor = self.build()
        self._failed_collection_webhook(entity, customer)

        resp = self.client.get(f"/v1/payments/webhooks/summary/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["failed"], 1)
        self.assertEqual(resp.json()["data"]["needs_attention"], 1)

    def test_another_entitys_failure_is_not_listed(self):
        entity, customer, _vendor = self.build()
        self._failed_collection_webhook(entity, customer)

        other = LedgerEntity.objects.create(
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT)
        seed_chart_of_accounts(other)
        resp = self.client.get(f"/v1/payments/webhooks/?entity={other.code}")
        self.assertEqual(resp.status_code, 200)
        # The paginated envelope keeps an empty page as a list (the []→{} coercion is
        # on success_response, which this view does not take).
        self.assertEqual(resp.json()["data"], [])

    def test_listing_requires_the_permission(self):
        entity, customer, _vendor = self.build()
        self._failed_collection_webhook(entity, customer)

        resp = self._unprivileged_client().get(
            f"/v1/payments/webhooks/?entity={entity.code}")
        self.assertEqual(resp.status_code, 403)

    def test_replay_requires_the_permission(self):
        entity, customer, _vendor = self.build()
        _intent, event = self._failed_collection_webhook(entity, customer)

        resp = self._unprivileged_client().post(
            f"/v1/payments/webhooks/{event.id}/replay/?entity={entity.code}")
        self.assertEqual(resp.status_code, 403)

    def test_replay_books_exactly_one_receipt_once_a_period_is_open(self):
        entity, customer, _vendor = self.build()
        intent, event = self._failed_collection_webhook(entity, customer)
        FiscalPeriod.objects.filter(entity=entity).update(status="OPEN")

        resp = self.client.post(
            f"/v1/payments/webhooks/{event.id}/replay/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200, resp.content)
        event.refresh_from_db()
        self.assertEqual(event.status, "PROCESSED")
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

        # Pressing it again must not book a second receipt.
        again = self.client.post(
            f"/v1/payments/webhooks/{event.id}/replay/?entity={entity.code}")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_replay_that_still_cannot_book_says_so(self):
        entity, customer, _vendor = self.build()
        _intent, event = self._failed_collection_webhook(entity, customer)

        resp = self.client.post(  # Periods are still closed.
            f"/v1/payments/webhooks/{event.id}/replay/?entity={entity.code}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("did not succeed", resp.json()["message"])
        event.refresh_from_db()
        self.assertEqual(event.status, "FAILED")
        self.assertFalse(Payment.objects.filter(entity=entity).exists())


class UnattributedWebhookVisibilityTests(_PaymentsFixtureMixin, TestCase):
    """An event that matched nothing belongs to no tenant, and still has to be seen.

    ``FailedWebhookVisibilityTests`` above covers the events we *can* attribute: they
    reach an entity through the collection or payout they matched, and that entity's
    operators see them. An event that matched neither has no entity at all, so it
    appeared on no screen - and it is not nothing, it is money moving at the provider
    against a reference we do not recognise (a staging PSP pointed at production, a
    deleted reference, an event type we do not handle).

    It cannot simply be shown to every tenant: the reference belongs to one of them, and
    showing it to the others leaks a transaction. So it lives on a platform-scope list
    that only CX staff may open, gated by its own permission keys rather than the
    entity-scoped ``payments.webhook.*`` grants, which are a tenant's licence over its
    own books.
    """

    LIST_URL = "/v1/payments/webhooks/unattributed/"

    def setUp(self):
        from django.core.management import call_command
        from django.contrib.auth import get_user_model
        from vs_tenants.models import Tenant

        call_command("seed_actions", verbosity=0)
        call_command("seed_payments_permissions", verbosity=0)

        self.User = get_user_model()
        self.Tenant = Tenant
        self.codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)

    # -- actors ------------------------------------------------------------- #

    def _client_holding(self, *keys, email, tenant=None):
        """A live client for a user holding exactly *keys* in *tenant* (codex default).

        Deliberately never the super admin: ``is_vision_super_admin`` bypasses every
        RBAC check, which would hide whether the new keys are enforced at all.
        """
        from core.test_utils import TenantAPIClient
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
        )

        tenant = tenant or self.codex
        user = self.User.objects.create_user(
            email=email, password="testpass123",
            status="ACTIVE", first_name="Hook", last_name="Tester", tenant=tenant,
        )
        role = make_role(tenant, name=f"Role {email}")
        for key in keys:
            make_role_permission(role, make_permission(key))
        make_assignment(tenant, user, role)
        return TenantAPIClient(user=user)

    def _platform_operator(self, email="unattributed-ops@test.com"):
        """CX staff holding both new platform-scope keys."""
        return self._client_holding(
            "payments.unattributed_webhook.view",
            "payments.unattributed_webhook.replay",
            email=email,
        )

    def _school_tenant(self, slug="hook-school"):
        return self.Tenant.objects.create(
            name="Hook School", slug=slug,
            kind=self.Tenant.Kind.SCHOOL, status=self.Tenant.Status.ACTIVE,
        )

    # -- events ------------------------------------------------------------- #

    def _unmatched_charge(self, *, reference="STRAY-1", amount=50000):
        """Ingest a signed charge for a reference we never issued; returns the event."""
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="SUCCEEDED", amount=amount)
        with self.captureOnCommitCallbacks(execute=True):  # Run the deferred worker inline.
            event = webhooks.ingest_webhook(provider="FAKE", raw_body=raw, headers=headers)
        event.refresh_from_db()
        return event

    def _deposit_to_unknown_account(self, *, account_number, reference, amount):
        """Ingest a deposit naming a NUBAN we have not provisioned; returns the event."""
        self.fake.forced_status[reference] = "SUCCEEDED"  # The provider's own API agrees.
        self.fake.forced_amount[reference] = amount  # ... and reports the settled amount.
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=reference, status="SUCCEEDED",
            amount=amount, receiver_account_number=account_number)
        with self.captureOnCommitCallbacks(execute=True):
            event = webhooks.ingest_webhook(provider="FAKE", raw_body=raw, headers=headers)
        event.refresh_from_db()
        return event

    def _attributed_failure(self, entity, customer):
        """Drive a real collection webhook that fails to book; returns the event.

        Mirrors ``FailedWebhookVisibilityTests._failed_collection_webhook``: the event
        is linked to a collection (and therefore to an entity) even though the booking
        blew up, which is exactly what must keep it off the platform list.
        """
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, narration="Fees")
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        FiscalPeriod.objects.filter(entity=entity).update(status="CLOSED")
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=intent.reference,
            status="SUCCEEDED", amount=50000)
        event = webhooks.ingest_webhook(provider="FAKE", raw_body=raw, headers=headers)
        webhooks.process_stored_event(event.id)
        event.refresh_from_db()
        return event

    # -- what the list contains --------------------------------------------- #

    def test_an_unattributed_event_is_listed_for_the_platform(self):
        self.build()
        event = self._unmatched_charge()

        self.assertEqual(event.status, "IGNORED")
        self.assertIsNone(event.collection_id)
        self.assertIsNone(event.payout_id)

        resp = self._platform_operator().get(self.LIST_URL)

        self.assertEqual(resp.status_code, 200, resp.content)
        rows = resp.json()["data"]
        self.assertEqual([r["id"] for r in rows], [event.id])
        self.assertEqual(rows[0]["error"], "No matching collection intent.")  # Why it is here.
        self.assertIsNone(rows[0]["target_kind"])  # Nothing local to point at.
        # The provider's raw record is the provider's, not the console's.
        for leaked in ("payload", "raw_body", "headers", "signature"):
            self.assertNotIn(leaked, rows[0])

    def test_an_event_that_belongs_to_an_entity_is_not_on_the_platform_list(self):
        entity, customer, _vendor = self.build()
        attributed = self._attributed_failure(entity, customer)
        stray = self._unmatched_charge(reference="STRAY-2")

        resp = self._platform_operator().get(self.LIST_URL)

        self.assertEqual(resp.status_code, 200, resp.content)
        listed = [r["id"] for r in resp.json()["data"]]
        self.assertEqual(listed, [stray.id])  # The entity's own event has its own screen.
        self.assertNotIn(attributed.id, listed)

    def test_the_quiet_case_is_an_empty_list_not_an_error(self):
        self.build()

        resp = self._platform_operator().get(self.LIST_URL)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"], [])

    def test_status_all_widens_the_list_beyond_the_terminal_states(self):
        # A stored-but-not-yet-processed event is unattributed too, but it is in flight,
        # not debris - so it stays out of the default view and shows under ?status=ALL.
        self.build()
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference="STRAY-3", status="SUCCEEDED", amount=1000)
        event = webhooks.ingest_webhook(  # No on-commit capture: the worker never runs.
            provider="FAKE", raw_body=raw, headers=headers)
        self.assertEqual(event.status, "RECEIVED")

        client = self._platform_operator()
        self.assertEqual(client.get(self.LIST_URL).json()["data"], [])
        widened = client.get(f"{self.LIST_URL}?status=ALL")
        self.assertEqual([r["id"] for r in widened.json()["data"]], [event.id])

    # -- who may open it ----------------------------------------------------- #

    def test_a_tenant_user_is_refused_even_holding_the_entity_scoped_keys(self):
        """The whole point of the screen: it spans tenants, so no tenant may open it.

        This user holds every payments webhook key there is, including the two new
        platform ones. Their home tenant is a school, so the platform gate refuses them
        before RBAC is consulted - a tenant cannot be granted its way into a list of
        other tenants' references.
        """
        self.build()
        event = self._unmatched_charge()
        client = self._client_holding(
            "payments.webhook.view", "payments.webhook.replay",
            "payments.unattributed_webhook.view", "payments.unattributed_webhook.replay",
            email="school-hooks@test.com", tenant=self._school_tenant(),
        )

        self.assertEqual(client.get(self.LIST_URL).status_code, 403)
        self.assertEqual(
            client.post(f"{self.LIST_URL}{event.id}/replay/").status_code, 403)

    def test_a_platform_user_without_the_new_key_is_refused(self):
        """Holding the entity-scoped webhook keys is not holding these ones."""
        self.build()
        event = self._unmatched_charge()
        client = self._client_holding(
            "payments.webhook.view", "payments.webhook.replay",
            email="cx-entity-hooks@test.com",
        )

        self.assertEqual(client.get(self.LIST_URL).status_code, 403)
        self.assertEqual(
            client.post(f"{self.LIST_URL}{event.id}/replay/").status_code, 403)

    def test_the_replay_key_alone_does_not_open_the_list(self):
        self.build()
        client = self._client_holding(
            "payments.unattributed_webhook.replay", email="cx-replay-only@test.com")

        self.assertEqual(client.get(self.LIST_URL).status_code, 403)

    # -- replay -------------------------------------------------------------- #

    def test_replay_books_the_deposit_once_the_account_is_provisioned(self):
        """The realistic reason an unattributed event becomes attributable.

        A payer transfers into a NUBAN the console has no record of, so nothing resolves
        it and the cash sits unbooked. Once an operator provisions that account, the
        stored event has everything it needs: replaying it resolves the payer, books the
        receipt, and the event leaves this list for the entity's own screen.
        """
        entity, customer, _vendor = self.build()
        event = self._deposit_to_unknown_account(
            account_number="9988776655", reference="DEP-STRAY", amount=45000)
        self.assertEqual(event.status, "IGNORED")
        self.assertIn("9988776655", event.error)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

        VirtualAccount.objects.create(  # The account is provisioned after the fact.
            entity=entity, provider="FAKE", customer=customer,
            account_number="9988776655", bank_name="Fake MFB", account_name="Acme Ltd",
            deposit_account=Account.objects.get(entity=entity, code="1110"),
        )
        client = self._platform_operator()
        resp = client.post(f"{self.LIST_URL}{event.id}/replay/")

        self.assertEqual(resp.status_code, 200, resp.content)
        event.refresh_from_db()
        self.assertEqual(event.status, "PROCESSED")
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)
        # Now attributable, so it belongs to the entity's screen and not this one.
        self.assertIsNotNone(event.collection_id)
        self.assertEqual(client.get(f"{self.LIST_URL}?status=ALL").json()["data"], [])

    def test_replaying_twice_books_nothing_further(self):
        entity, customer, _vendor = self.build()
        event = self._deposit_to_unknown_account(
            account_number="9988776600", reference="DEP-STRAY-2", amount=30000)
        VirtualAccount.objects.create(
            entity=entity, provider="FAKE", customer=customer,
            account_number="9988776600", bank_name="Fake MFB", account_name="Acme Ltd",
            deposit_account=Account.objects.get(entity=entity, code="1110"),
        )
        client = self._platform_operator()

        first = client.post(f"{self.LIST_URL}{event.id}/replay/")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

        # The event is no longer unattributed, so the platform endpoint no longer owns
        # it: pressing replay again from a stale screen must 404 rather than reach into
        # the entity's records, and must certainly not book a second receipt.
        again = client.post(f"{self.LIST_URL}{event.id}/replay/")
        self.assertEqual(again.status_code, 404)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_replay_that_resolves_nothing_says_so_and_changes_nothing(self):
        entity, _customer, _vendor = self.build()
        event = self._unmatched_charge(reference="STRAY-4")

        resp = self._platform_operator().post(f"{self.LIST_URL}{event.id}/replay/")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("did not succeed", resp.json()["message"])
        event.refresh_from_db()
        self.assertEqual(event.status, "IGNORED")
        self.assertIsNone(event.collection_id)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())


class UnbookedReceiptAlertTests(_PaymentsFixtureMixin, TestCase):
    """A screen is somewhere you look; an alert is something that tells you.

    Failed bookings were already recorded and shown, but nothing announced them, so a
    payment that failed on a Friday sat unnoticed all weekend. These pin the two
    alarms: a standing daily digest per entity, and an incident alarm for platform
    staff when several fail inside one window.
    """

    def setUp(self):
        from django.core.management import call_command

        call_command("seed_actions", verbosity=0)
        call_command("seed_payments_permissions", verbosity=0)

    def _unbooked(self, entity, customer, *, count=1, status="FAILED", error="",
                  amount=50000):
        """Create ``count`` webhook events that failed to book, attributed to ``entity``."""
        from vs_payments.models import CollectionIntent, WebhookEvent

        made = []
        for _ in range(count):
            # A CollectionIntent reference is unique platform-wide, so the counter has
            # to advance across calls within one test, not just within one loop.
            self._seq = getattr(self, "_seq", 0) + 1
            intent = CollectionIntent.objects.create(
                entity=entity, customer=customer, provider="FAKE",
                reference=f"UNBOOKED-{self._seq}",
                amount=amount, status=CollectionStatus.PROCESSING,
            )
            made.append(WebhookEvent.objects.create(
                provider="FAKE", event_type="charge.success",
                dedupe_key=f"DK-{intent.reference}", verified=True,
                status=status, collection=intent,
                error=error or "Cannot post into period 'Aug 2026': it is 'CLOSED'.",
            ))
        return made

    # --- the daily digest ---------------------------------------------------- #

    def test_a_clean_day_says_nothing(self):
        """Silence is the correct output when nothing is outstanding."""
        from vs_payments.alerts import unbooked_digest

        self.build()
        self.assertEqual(unbooked_digest(),
                         {"entities": 0, "events": 0, "notified": 0})

    def test_outstanding_events_are_reported_once_per_entity(self):
        """One message for three failures, not three messages."""
        from unittest.mock import patch

        from vs_payments.alerts import unbooked_digest

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer, count=3)

        with patch("vs_payments.alerts._notify", return_value=["n1"]) as notify:
            summary = unbooked_digest()

        self.assertEqual(summary["entities"], 1)
        self.assertEqual(summary["events"], 3)
        self.assertEqual(notify.call_count, 1)  # per entity, never per event

    def test_the_message_names_the_money_and_the_reason(self):
        """"3 payments, no fiscal period open" is actionable; "3 failures" is not."""
        from unittest.mock import patch

        from vs_payments.alerts import unbooked_digest

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer, count=2, amount=50000)

        with patch("vs_payments.alerts._notify", return_value=[]) as notify:
            unbooked_digest()

        context = notify.call_args.kwargs["context"]
        self.assertEqual(context["count"], 2)
        self.assertEqual(context["total_amount"], 100000)
        self.assertIn("CLOSED", context["reason"])
        self.assertEqual(context["entity_code"], entity.code)

    def test_a_booked_event_is_not_reported(self):
        from unittest.mock import patch

        from vs_payments.alerts import unbooked_digest

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer, status="PROCESSED")

        with patch("vs_payments.alerts._notify", return_value=[]) as notify:
            self.assertEqual(unbooked_digest()["events"], 0)
        notify.assert_not_called()

    def test_one_bad_entity_does_not_abort_the_sweep(self):
        """A sweep that dies on the first tenant is worse than no sweep."""
        from unittest.mock import patch

        from vs_finance.models import LedgerEntity
        from vs_payments.alerts import unbooked_digest

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer)
        other = LedgerEntity.objects.create(
            name="Second Books", code="SECOND", kind=LedgerEntity.Kind.TENANT,
            tenant=entity.tenant)
        seed_chart_of_accounts(other)
        second_customer = Customer.objects.create(
            entity=other, code="CUST2", name="Beta Ltd",
            receivable_account=Account.objects.get(entity=other, code="1200"))
        self._unbooked(other, second_customer)

        with patch("vs_payments.alerts._notify", side_effect=[RuntimeError("boom"), ["ok"]]):
            summary = unbooked_digest()

        self.assertEqual(summary["entities"], 2)  # both were attempted

    # --- the surge alarm ------------------------------------------------------ #

    def test_a_single_failure_is_not_a_surge(self):
        """One bad payment is not an incident; the digest will carry it tomorrow."""
        from vs_payments.alerts import unbooked_surge

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer, count=1)
        self.assertFalse(unbooked_surge()["alarmed"])

    def test_several_failures_in_one_window_alarm_the_platform(self):
        from unittest.mock import patch

        from vs_payments.alerts import unbooked_surge

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer, count=3)

        with patch("vs_payments.alerts._notify", return_value=["n1"]) as notify:
            summary = unbooked_surge()

        self.assertTrue(summary["alarmed"])
        self.assertEqual(summary["failures"], 3)
        context = notify.call_args.kwargs["context"]
        self.assertIn("CLOSED", context["reason"])
        self.assertIn(entity.code, context["entities"])

    def test_failures_outside_the_window_do_not_alarm(self):
        """Windowed on purpose: a resolved outage goes quiet by itself."""
        import datetime

        from django.utils import timezone

        from vs_payments.alerts import unbooked_surge
        from vs_payments.models import WebhookEvent

        entity, customer, _vendor = self.build()
        events = self._unbooked(entity, customer, count=5)
        stale = timezone.now() - datetime.timedelta(hours=4)
        WebhookEvent.objects.filter(pk__in=[e.pk for e in events]).update(created_at=stale)

        self.assertFalse(unbooked_surge()["alarmed"])

    def test_unattributable_failures_still_alarm(self):
        """These are the ones no entity-scoped screen shows, so they matter most."""
        from unittest.mock import patch

        from vs_payments.alerts import unbooked_surge
        from vs_payments.models import WebhookEvent

        self.build()
        for i in range(3):
            WebhookEvent.objects.create(
                provider="FAKE", event_type="charge.success",
                dedupe_key=f"ORPHAN-{i}", verified=True, status="IGNORED",
                error="No matching collection intent.",
            )
        with patch("vs_payments.alerts._notify", return_value=["n1"]) as notify:
            summary = unbooked_surge()

        self.assertTrue(summary["alarmed"])
        self.assertIn("unattributed", notify.call_args.kwargs["context"]["entities"])

    def test_a_delivery_failure_never_breaks_the_alarm(self):
        """An alarm that crashes its own task is the opposite of an alarm."""
        from unittest.mock import patch

        from vs_payments.alerts import unbooked_digest

        entity, customer, _vendor = self.build()
        self._unbooked(entity, customer)

        with patch("vs_notifications.notify.send_notification",
                   side_effect=RuntimeError("smtp down")):
            summary = unbooked_digest()  # must not raise

        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["notified"], 0)

    def _permission_holder(self, entity, permission_key):
        """A user who genuinely holds ``permission_key`` in this entity's tenant.

        Built through the RBAC tables rather than a super-admin shortcut, because
        the point of the assertion is that the alert reaches the people the platform
        considers able to act, and a bypass would prove nothing about that.
        """
        from django.contrib.auth import get_user_model
        from vs_rbac.models import (
            Permission, TenantRolePermission, TenantRoleTemplate,
            TenantUserRoleAssignment,
        )

        user = get_user_model().objects.create_user(
            email=f"holder-{permission_key}@test.com", password="pw",
            tenant=entity.tenant, status="ACTIVE",
            first_name="Alert", last_name="Holder",
        )
        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=entity.tenant, key="alert-holder",
            defaults={"name": "Alert Holder", "status": "ACTIVE"},
        )
        TenantRolePermission.objects.get_or_create(
            role=role, permission=Permission.objects.get(key=permission_key),
            defaults={"granted": True},
        )
        TenantUserRoleAssignment.objects.create(
            tenant=entity.tenant, user=user, role=role, assignment_status="ACTIVE")
        return user

    def test_both_alarms_have_templates_on_every_channel(self):
        """A registered event with no template dispatches nothing, silently.

        This is not hypothetical: both alarms shipped registered, with recipients
        resolving correctly, and delivered nothing at all because no default template
        existed. The dispatcher logs "no active template - channel skipped" and
        returns success, so the only symptom is silence, which is indistinguishable
        from the good case an alarm is supposed to detect.
        """
        from django.core.management import call_command

        from vs_notifications.models import NotificationEventType, NotificationTemplate

        call_command("seed_notification_templates", verbosity=0)
        for key in ("payments.unbooked_receipts_digest",
                    "payments.unbooked_receipts_surge"):
            event_type = NotificationEventType.objects.get(key=key)
            for channel in event_type.supported_channels:
                self.assertTrue(
                    NotificationTemplate.objects.filter(
                        event_type=event_type, channel=channel).exists(),
                    f"{key} has no {channel} template, so it would deliver nothing.",
                )

    def test_the_digest_actually_delivers(self):
        """End to end through the real notification stack, not a mock.

        The mocked tests above prove the sweep's arithmetic; this proves the alarm
        reaches a person. Without it, every template, recipient and registry mistake
        looks exactly like a quiet day.
        """
        from django.core.management import call_command

        from vs_notifications.models import Notification

        call_command("seed_notification_templates", verbosity=0)
        entity, customer, _vendor = self.build()
        recipient = self._permission_holder(entity, "payments.webhook.view")
        self._unbooked(entity, customer, count=2, amount=25000)

        from vs_payments.alerts import unbooked_digest
        summary = unbooked_digest()

        self.assertEqual(summary["events"], 2)
        delivered = Notification.objects.filter(
            event_type__key="payments.unbooked_receipts_digest")
        self.assertTrue(delivered.exists(), "the digest reached nobody")
        self.assertEqual({n.recipient_id for n in delivered}, {recipient.pk})
        in_app = delivered.get(channel="in_app").body
        email = delivered.get(channel="email").body
        self.assertIn("2 gateway payment", in_app)
        self.assertIn("Payments affected: 2", email)
        self.assertIn("₦500.00", in_app)  # 2 x 25,000 kobo
        for body in (in_app, email):
            # format_naira already carries the symbol; a literal one doubles it.
            self.assertNotIn("₦₦", body)


class PayoutOnboardingSeedTests(TestCase):
    """The payout gate arrives with the tenant's books.

    The provider boundary already fails closed without a template. Provisioning still
    supplies a usable tenant ladder automatically, so the first payout parks for an
    appointed checker instead of becoming an operational dead end.
    """

    def _tenant(self, slug="cedar-onboard", code="CDRON"):
        from vs_tenants.models import Branch, Tenant

        tenant = Tenant.objects.create(
            name="Cedar", slug=slug, kind=Tenant.Kind.SCHOOL,
            status=Tenant.Status.ACTIVE,
        )
        Branch.objects.create(
            tenant=tenant, name="Main", is_main=True, status="ACTIVE",
        )
        return tenant

    def _entity(self, tenant, code):
        from vs_finance.models import LedgerEntity

        return LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT,
            tenant=tenant,
        )

    def test_creating_books_publishes_the_payout_ladder(self):
        from vs_finance.provisioning import provision_entity
        from vs_workflow.models import WorkflowTemplate

        tenant = self._tenant()
        provision_entity(self._entity(tenant, "CDRBK"))

        self.assertTrue(WorkflowTemplate.all_objects.filter(
            tenant=tenant, document_type="payments.payout_batch").exists())

    def test_the_seeded_stage_never_auto_skips(self):
        """An unstaffed stage must park the batch, not pay it out."""
        from vs_finance.provisioning import provision_entity
        from vs_workflow.models import WorkflowTemplate

        tenant = self._tenant(slug="elm-onboard", code="ELMON")
        provision_entity(self._entity(tenant, "ELMBK"))

        template = WorkflowTemplate.all_objects.get(
            tenant=tenant, document_type="payments.payout_batch")
        stages = template.stages.filter(retired_at__isnull=True)
        self.assertTrue(stages.exists())
        for stage in stages:
            self.assertFalse(stage.skip_if_no_approvers, stage.code)

    def test_the_approving_role_exists_and_nobody_holds_it(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_finance.provisioning import provision_entity

        from vs_payments.constants import WF_DEFAULT_APPROVE_ROLE

        tenant = self._tenant(slug="fir-onboard", code="FIRON")
        provision_entity(self._entity(tenant, "FIRBK"))

        role = TenantRoleTemplate.objects.get(
            tenant=tenant, key=WF_DEFAULT_APPROVE_ROLE)
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(role=role).exists())

    def test_provisioning_a_second_entity_changes_nothing(self):
        from vs_finance.provisioning import provision_entity
        from vs_workflow.models import WorkflowTemplate

        tenant = self._tenant(slug="gum-onboard", code="GUMON")
        provision_entity(self._entity(tenant, "GUMB1"))
        before = set(WorkflowTemplate.all_objects.filter(
            tenant=tenant).values_list("pk", flat=True))

        provision_entity(self._entity(tenant, "GUMB2"))
        after = set(WorkflowTemplate.all_objects.filter(
            tenant=tenant).values_list("pk", flat=True))
        self.assertEqual(before, after)
