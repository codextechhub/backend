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

from django.test import TestCase

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
from .exceptions import DuplicateWebhookError, PaymentStateError, WebhookSignatureError
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
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        self.assertEqual(event.status, "PROCESSED")
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    # Verify duplicate webhook never double books behavior.
    def test_duplicate_webhook_never_double_books(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=25000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # provider verify agrees
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=25000,
        )
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        # The provider retries the exact same event.
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
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        intent.refresh_from_db()
        self.assertNotEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())


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
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(VendorPayment.objects.filter(entity=entity).count(), 1)

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
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        User = get_user_model()
        self.user = User.objects.create_user(
            email="pay-admin@test.com", password="testpass123",
            user_type="CX_STAFF", status="ACTIVE",
            first_name="Pay", last_name="Admin",
        )
        role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.user, role=role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

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
        first = self.client.post(
            "/v1/payments/webhooks/PAYSTACK/", data=raw,
            content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)
        # A retry is acknowledged (200) but does not re-book.
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
            PlatformRoleTemplate, PlatformUserRoleAssignment,
            SchoolRolePermission, SchoolRoleTemplate, SchoolUserRoleAssignment,
        )
        from vs_schools.models import School

        call_command("seed_payments_permissions", verbosity=0, stdout=io.StringIO())

        self.User = get_user_model()
        self.SchoolRoleTemplate = SchoolRoleTemplate
        self.SchoolRolePermission = SchoolRolePermission
        self.SchoolUserRoleAssignment = SchoolUserRoleAssignment

        # A school-owned entity so batch.school resolves and SCHOOL-scoped approver
        # resolution has a pool to draw from.
        self.school = School.objects.create(name="Cedar", slug="cedar-pba", code="CDRPBA")
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Cedar Books", code="CDRBK", kind=LedgerEntity.Kind.TENANT,
            source_school=self.school,
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

        # Requester: a CX super admin (bypasses the endpoint RBAC gate, sees every
        # entity). SoD still excludes them from approving their own batch.
        self.requester = self.User.objects.create_user(
            email="req-pba@test.com", password="pw", user_type="CX_STAFF", status="ACTIVE",
            first_name="Req", last_name="Ester",
        )
        super_role = PlatformRoleTemplate.objects.create(id="xvs_super_admin", name="Super Admin")
        PlatformUserRoleAssignment.objects.create(
            user=self.requester, role=super_role, assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.requester)

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
            school=self.school, branch=None,
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
            first_name="Apr", last_name="Over", school=self.school,
        )
        role, _ = self.SchoolRoleTemplate.objects.get_or_create(
            id="pba-checker", defaults={"school": self.school, "name": "Payout Checker"},
        )
        self.SchoolRolePermission.objects.get_or_create(
            role=role, permission_id=self.APPROVE_KEY, defaults={"granted": True},
        )
        self.SchoolUserRoleAssignment.objects.create(
            school=self.school, user=user, role=role, assignment_status="ACTIVE",
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
