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

from vs_finance.accounts import resolve_account
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

from . import services, webhooks
from .constants import CollectionStatus, PayoutStatus
from .exceptions import DuplicateWebhookError, WebhookSignatureError
from .models import CollectionIntent, PaymentEvent, PayoutInstruction
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
