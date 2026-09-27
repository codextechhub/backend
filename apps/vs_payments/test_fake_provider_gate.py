"""The Fake payment provider exists only where a settings module switches it on.

``FAKE`` moves no money: its checkout links and account numbers are made up, and an
event it "confirms" is a claim nobody can check. ``PAYMENTS_FAKE_PROVIDER_ENABLED`` is
off in ``base.py`` and on only in the development and test settings modules, and
:mod:`vs_payments.providers.registry` is the one gate every caller resolves through.

The disabled cases are the security-critical ones and come first: with the flag off,
nothing can start, receive, confirm or replay a Fake payment, and rows already on file
still read. The enabled cases prove the gate did not break the provider where it is
wanted.

Run from ``apps/``:
    ../cx/bin/python manage.py test vs_payments.test_fake_provider_gate --settings=apps.settings.local
"""
from __future__ import annotations

import hashlib
import hmac
import json

from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from vs_finance.models import Payment
from vs_procurement.models import VendorPayment

from . import services, webhooks
from .constants import CollectionChannel, CollectionStatus, PayoutStatus, VirtualAccountStatus
from .exceptions import ProviderNotConfiguredError
from .models import (
    CollectionIntent,
    PaymentEvent,
    PayoutBatch,
    PayoutInstruction,
    VirtualAccount,
    WebhookEvent,
)
from .providers import registry
from .providers.fake import FakeProvider
from .tests import _PaymentsFixtureMixin, _platform_tenant


def _api_client():
    """An API client signed in as a CX super admin, who holds every payments key."""
    from django.contrib.auth import get_user_model

    from core.test_utils import TenantAPIClient
    from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

    platform = _platform_tenant()
    user = get_user_model().objects.create_user(
        tenant=platform, email="fake-gate@test.com", password="testpass123",
        status="ACTIVE", first_name="Fake", last_name="Gate",
    )
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=platform, key="xvs_super_admin",
        defaults={"name": "Super Admin", "status": "ACTIVE"},
    )
    TenantUserRoleAssignment.objects.create(
        tenant=platform, user=user, role=role, assignment_status="ACTIVE",
    )
    return TenantAPIClient(user=user)


def _signed_body(secret: str, reference: str, amount: int) -> tuple[bytes, str]:
    """A ``charge.success`` body and its Fake signature under ``secret``."""
    body = json.dumps({"event": "charge.success", "data": {
        "reference": reference, "status": "SUCCEEDED", "amount": amount, "id": "1",
    }}).encode()
    return body, hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def _legacy_fake_collection(entity, customer, invoice, *, amount):
    """A FAKE collection already on file, as a database from before the gate holds."""
    return CollectionIntent.objects.create(
        entity=entity, provider="FAKE", channel=CollectionChannel.CHECKOUT,
        reference=services._new_reference(entity), provider_reference="FAKE-legacy",
        amount=amount, currency=entity.base_currency, customer=customer, invoice=invoice,
        checkout_url="https://fake.test/checkout/legacy",
        status=CollectionStatus.PROCESSING,
    )


@override_settings(PAYMENTS_FAKE_PROVIDER_ENABLED=False)
class FakeProviderDisabledTests(_PaymentsFixtureMixin, TestCase):
    """With the flag off, FAKE is a provider that does not exist."""

    def test_the_provider_list_omits_fake(self):
        self.assertEqual(registry.available_providers(), ("PAYSTACK",))
        self.assertFalse(registry.is_available("FAKE"))
        self.assertFalse(registry.is_available("fake"))
        self.assertTrue(registry.is_available("paystack"))

    def test_get_provider_refuses_fake_even_when_an_override_is_registered(self):
        self.build()  # Registers self.fake under FAKE and PAYSTACK.
        with self.assertRaises(ProviderNotConfiguredError):
            registry.get_provider("FAKE")
        self.assertIs(registry.get_provider("PAYSTACK"), self.fake)

    def test_a_forged_fake_webhook_is_404_and_stores_books_and_audits_nothing(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=50000)
        intent = _legacy_fake_collection(entity, customer, invoice, amount=50000)
        # Had the event got through, verification would have agreed it was paid.
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        client = APIClient()  # Anonymous, as a provider calls it.

        for secret in ("fake-secret", "test-secret"):
            body, sig = _signed_body(secret, intent.reference, 50000)
            for name in ("FAKE", "fake"):
                with self.captureOnCommitCallbacks(execute=True):
                    resp = client.post(
                        f"/v1/payments/webhooks/{name}/", data=body,
                        content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,
                    )
                self.assertEqual(resp.status_code, 404, resp.content)

        self.assertFalse(WebhookEvent.objects.exists())
        self.assertFalse(PaymentEvent.objects.filter(action__startswith="WEBHOOK").exists())
        self.assertFalse(Payment.objects.filter(entity=entity).exists())
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)

    def test_starting_a_collection_with_fake_is_refused(self):
        entity, customer, _ = self.build()
        with self.assertRaises(ValidationError) as caught:
            services.initiate_collection(
                entity=entity, amount=10000, customer=customer, provider="FAKE",
            )
        self.assertIn("provider", caught.exception.detail)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_provisioning_a_virtual_account_with_fake_is_refused(self):
        entity, customer, _ = self.build()
        with self.assertRaises(ValidationError) as caught:
            services.create_virtual_account(entity=entity, customer=customer, provider="fake")
        self.assertIn("provider", caught.exception.detail)
        self.assertFalse(VirtualAccount.objects.exists())

    def test_creating_a_payout_batch_with_fake_is_refused(self):
        entity, _, vendor = self.build()
        with self.assertRaises(ValidationError) as caught:
            services.create_payout_batch(
                entity=entity, items=[{"amount": 7000, "vendor": vendor}], provider="FAKE",
            )
        self.assertIn("provider", caught.exception.detail)
        self.assertFalse(PayoutBatch.objects.exists())

    def test_the_api_refuses_fake_with_a_400_on_provider(self):
        entity, customer, _ = self.build()
        client = _api_client()
        collection = client.post(
            f"/v1/payments/collections/?entity={entity.code}",
            {"amount": 10000, "customer": customer.pk, "provider": "FAKE"}, format="json",
        )
        self.assertEqual(collection.status_code, 400, collection.content)
        self.assertIn("provider", collection.content.decode())
        account = client.post(
            f"/v1/payments/virtual-accounts/?entity={entity.code}",
            {"customer": customer.pk, "provider": "FAKE"}, format="json",
        )
        self.assertEqual(account.status_code, 400, account.content)
        self.assertFalse(CollectionIntent.objects.exists())
        self.assertFalse(VirtualAccount.objects.exists())

    @override_settings(PAYMENTS_DEFAULT_PROVIDER="FAKE")
    def test_a_fake_default_provider_is_a_configuration_fault(self):
        entity, customer, _ = self.build()
        with self.assertRaises(ProviderNotConfiguredError):
            services.initiate_collection(entity=entity, amount=10000, customer=customer)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_fake_rows_already_on_file_still_list_and_read(self):
        entity, customer, _ = self.build()
        intent = _legacy_fake_collection(entity, customer, None, amount=20000)
        VirtualAccount.objects.create(
            entity=entity, provider="FAKE", customer=customer, account_number="0123456789",
            bank_name="Fake MFB", account_name="Acme Ltd", currency=entity.base_currency,
            provider_reference="FAKE-VA-legacy", status=VirtualAccountStatus.ACTIVE,
        )
        client = _api_client()

        listed = client.get(f"/v1/payments/collections/?entity={entity.code}")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual([row["provider"] for row in listed.json()["data"]], ["FAKE"])
        detail = client.get(f"/v1/payments/collections/{intent.pk}/?entity={entity.code}")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["provider"], "FAKE")
        accounts = client.get(f"/v1/payments/virtual-accounts/?entity={entity.code}")
        self.assertEqual(accounts.status_code, 200, accounts.content)
        self.assertEqual([row["provider"] for row in accounts.json()["data"]], ["FAKE"])

    def test_verifying_a_fake_collection_on_file_is_refused_and_books_nothing(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=20000)
        intent = _legacy_fake_collection(entity, customer, invoice, amount=20000)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        resp = _api_client().get(
            f"/v1/payments/collections/{intent.pk}/?entity={entity.code}&verify=1")
        self.assertEqual(resp.status_code, 503, resp.content)
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

    def test_confirming_a_fake_payout_on_file_is_refused_and_books_nothing(self):
        entity, _, vendor = self.build()
        payout = self.make_processing_payout(entity, vendor, amount=7000)
        PayoutInstruction.objects.filter(pk=payout.pk).update(provider="FAKE")
        self.fake.forced_status[payout.reference] = "PAID"
        with self.assertRaises(ProviderNotConfiguredError):
            services.confirm_payout(payout)
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertFalse(VendorPayment.objects.filter(entity=entity).exists())

    def test_a_stored_fake_event_fails_on_replay_and_books_nothing(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=30000)
        intent = _legacy_fake_collection(entity, customer, invoice, amount=30000)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        body, sig = _signed_body("test-secret", intent.reference, 30000)
        event = WebhookEvent.objects.create(
            provider="FAKE", event_type="charge.success", dedupe_key="FAKE:legacy",
            provider_reference=intent.reference, signature=sig, verified=True,
            status="RECEIVED", payload=json.loads(body), raw_body=body.decode(),
        )
        webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, "FAILED")
        self.assertIn("FAKE", event.error)
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())


class FakeProviderSecretTests(TestCase):
    """The Fake signing key comes from settings, and an empty one verifies nothing."""

    @override_settings(PAYMENTS_FAKE_PROVIDER_ENABLED=True, PAYMENTS_FAKE_WEBHOOK_SECRET="")
    def test_an_enabled_fake_with_no_secret_is_not_configured(self):
        registry.unregister()
        with self.assertRaises(ProviderNotConfiguredError):
            registry.get_provider("FAKE")

    def test_an_empty_secret_verifies_no_signature(self):
        body = b'{"event":"charge.success"}'
        forged = hmac.new(b"", body, hashlib.sha512).hexdigest()
        self.assertFalse(FakeProvider(secret="").verify_signature(
            raw_body=body, headers={"x-fake-signature": forged}))

    @override_settings(PAYMENTS_FAKE_PROVIDER_ENABLED=True,
                       PAYMENTS_FAKE_WEBHOOK_SECRET="from-settings")
    def test_the_built_provider_signs_with_the_configured_secret(self):
        registry.unregister()
        provider = registry.get_provider("fake")
        body, sig = _signed_body("from-settings", "R1", 100)
        self.assertTrue(provider.verify_signature(
            raw_body=body, headers={"x-fake-signature": sig}))
        _, old = _signed_body("fake-secret", "R1", 100)
        self.assertFalse(provider.verify_signature(
            raw_body=body, headers={"x-fake-signature": old}))


@override_settings(PAYMENTS_FAKE_PROVIDER_ENABLED=True)
class FakeProviderEnabledTests(_PaymentsFixtureMixin, TestCase):
    """With the flag on, the Fake provider works end to end as before."""

    def test_the_provider_list_includes_fake(self):
        self.assertEqual(registry.available_providers(), ("PAYSTACK", "FAKE"))

    def test_a_fake_collection_is_created_and_a_signed_webhook_books_it(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=40000)
        intent = services.initiate_collection(
            entity=entity, amount=40000, customer=customer, invoice=invoice, provider="fake",
        )
        self.assertEqual(intent.provider, "FAKE")
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        body, sig = _signed_body("test-secret", intent.reference, 40000)
        with self.captureOnCommitCallbacks(execute=True):
            resp = APIClient().post(
                "/v1/payments/webhooks/FAKE/", data=body,
                content_type="application/json", HTTP_X_FAKE_SIGNATURE=sig,
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_fake_virtual_account_is_provisioned(self):
        entity, customer, _ = self.build()
        va = services.create_virtual_account(entity=entity, customer=customer, provider="FAKE")
        self.assertEqual(va.provider, "FAKE")
        self.assertEqual(va.status, VirtualAccountStatus.ACTIVE)

    def test_an_unknown_provider_webhook_is_still_404(self):
        resp = APIClient().post(
            "/v1/payments/webhooks/NOPE/", data=b"{}", content_type="application/json")
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertFalse(WebhookEvent.objects.exists())
