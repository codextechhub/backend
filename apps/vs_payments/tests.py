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
from vs_finance.receivables import post_invoice
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.models import Vendor, VendorPayment

from . import reconciliation, services, webhooks
from .constants import CollectionStatus, PayoutBatchStatus, PayoutStatus
from .exceptions import DuplicateWebhookError, WebhookSignatureError
from .models import (
    CollectionIntent,
    PaymentEvent,
    PayoutBatch,
    PayoutInstruction,
)
from .providers import registry
from .providers.fake import FakeProvider


class _PaymentsFixtureMixin:
    """A seeded ledger entity with a customer, a vendor, and the Fake provider wired in.

    The fiscal year is built for *today's* year so a receipt/payout dated today always
    lands in an OPEN period (the booking date is ``date.today()``).
    """

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


class ProviderTests(_PaymentsFixtureMixin, TestCase):
    def test_fake_signature_roundtrip(self):
        self.build()
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference="R1", status="SUCCEEDED", amount=1000,
        )
        self.assertTrue(self.fake.verify_signature(raw_body=raw, headers=headers))
        # Tamper the body → signature no longer matches.
        self.assertFalse(self.fake.verify_signature(raw_body=raw + b"x", headers=headers))

    def test_registry_override_resolves_fake(self):
        self.build()
        self.assertIs(registry.get_provider("PAYSTACK"), self.fake)


class CollectionTests(_PaymentsFixtureMixin, TestCase):
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

    def test_failed_collection_books_nothing(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=9999, customer=customer)
        intent = services.confirm_collection(intent, status=CollectionStatus.FAILED)
        self.assertEqual(intent.status, CollectionStatus.FAILED)
        self.assertIsNone(intent.payment_id)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    def test_confirm_is_idempotent(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=20000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        # A second confirm must not book a second receipt.
        services.confirm_collection(intent, status=CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)


class WebhookTests(_PaymentsFixtureMixin, TestCase):
    def _signed(self, *, event, reference, status, amount=0):
        return self.fake.build_webhook(
            event=event, reference=reference, status=status, amount=amount,
        )

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

    def test_webhook_confirms_collection(self):
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=40000)
        intent = services.initiate_collection(
            entity=entity, amount=40000, customer=customer, invoice=inv,
        )
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=40000,
        )
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        self.assertEqual(event.status, "PROCESSED")
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_duplicate_webhook_never_double_books(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=25000, customer=customer)
        raw, headers = self._signed(
            event="charge.success", reference=intent.reference, status="SUCCEEDED", amount=25000,
        )
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        # The provider retries the exact same event.
        with self.assertRaises(DuplicateWebhookError):
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)


class PayoutTests(_PaymentsFixtureMixin, TestCase):
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

    def test_payout_webhook_confirms(self):
        entity, _, vendor = self.build()
        payout = services.initiate_payout(
            entity=entity, amount=15000, beneficiary_name="Supplier Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            vendor=vendor,
        )
        raw, headers = self.fake.build_webhook(
            event="transfer.success", reference=payout.reference, status="PAID", amount=15000,
        )
        webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(VendorPayment.objects.filter(entity=entity).count(), 1)

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


class _FlakyProvider(FakeProvider):
    """A FakeProvider that refuses to transfer a sentinel amount (to fail one item)."""

    def __init__(self, *, fail_amount, **kwargs):
        super().__init__(**kwargs)
        self.fail_amount = fail_amount

    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        if amount == self.fail_amount:
            from .exceptions import ProviderError
            raise ProviderError("Beneficiary account rejected by the bank.")
        return super().create_transfer(
            reference=reference, amount=amount, currency=currency,
            account_number=account_number, bank_code=bank_code,
            account_name=account_name, narration=narration, metadata=metadata,
        )


class PayoutBatchTests(_PaymentsFixtureMixin, TestCase):
    def _items(self, vendor, *amounts):
        return [
            {"amount": amt, "beneficiary_name": "Supplier Ltd",
             "beneficiary_account_number": f"012345678{i}", "beneficiary_bank_code": "058",
             "vendor": vendor}
            for i, amt in enumerate(amounts)
        ]

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

    def test_submit_batch_dispatches_every_item(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(entity=entity, items=self._items(vendor, 5000, 7000))
        batch = services.submit_payout_batch(batch)
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)
        self.assertIsNotNone(batch.submitted_at)
        self.assertTrue(
            all(p.status == PayoutStatus.PROCESSING for p in batch.instructions.all())
        )

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


class SettlementReconciliationTests(_PaymentsFixtureMixin, TestCase):
    def _bank_account(self, entity):
        from vs_finance.models import BankAccount
        return BankAccount.objects.create(
            entity=entity, name="Operations",
            gl_account=Account.objects.get(entity=entity, code="1100"),
        )

    def _bank_line(self, bank_account, *, amount, reference="", description="", day=None):
        from vs_finance.models import BankStatementLine
        return BankStatementLine.objects.create(
            bank_account=bank_account, txn_date=day or datetime.date.today(),
            description=description, reference=reference, amount=amount,
        )

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


class PaymentEventTests(_PaymentsFixtureMixin, TestCase):
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


class PaymentsAPITests(_PaymentsFixtureMixin, TestCase):
    """The /v1/payments/ REST surface, authenticated as a Vision super admin."""

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

    def test_collection_detail_verify_confirms(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=12000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        resp = self.client.get(
            f"/v1/payments/collections/{intent.pk}/?entity={entity.code}&verify=1"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], CollectionStatus.SUCCEEDED)

    def test_webhook_endpoint_processes_and_dedupes(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=33000, customer=customer)
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
