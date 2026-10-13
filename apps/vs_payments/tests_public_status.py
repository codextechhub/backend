"""The payer's return page learns how its payment went, and learns nothing else.

Mrs Adeyemi pays Corona Lekki's invoice for Tunde on her phone and is sent back to
her school's return page with the payment's reference in the address. The page asks
the public status route. It is told whether the payment is confirmed, how much, and
who it went to, and not Tunde's name, the invoice, her email or the branch. Someone
who guesses a reference gets the same 404 as for one that never existed.
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from vs_finance.models import Account, Customer

from . import services
from .constants import CollectionStatus
from .models import CollectionIntent
from .tests_custody_held import _HeldFixture
from .views_public import PublicCollectionReferenceThrottle

PAYLOAD = {"status", "amount", "amount_naira", "currency", "payee_name", "paid_at"}


class PublicCollectionStatusTests(_HeldFixture):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.anonymous = APIClient()

    def url(self, reference):
        return f"/v1/payments/public/collections/{reference}/status/"

    def started(self, entity=None, customer=None, amount=50_000, age_seconds=60):
        intent = services.initiate_collection(
            entity=entity or self.books, amount=amount, customer=customer or self.adeyemi,
            payer_email="mum@adeyemi.test", narration="Tunde term fees")
        CollectionIntent.objects.filter(pk=intent.pk).update(
            created_at=timezone.now() - datetime.timedelta(seconds=age_seconds))
        return CollectionIntent.objects.get(pk=intent.pk)

    def test_references_are_random_not_sequential(self):
        first, second = self.started(), self.started()
        self.assertRegex(first.reference, services.COLLECTION_REFERENCE_RE)
        self.assertNotEqual(first.reference.rsplit("-", 1)[1], second.reference.rsplit("-", 1)[1])

    def test_an_unknown_reference_is_a_plain_404(self):
        for reference in ("CXP-1-0000000000000000", "nonsense", "CXP-5"):
            with self.subTest(reference=reference):
                response = self.anonymous.get(self.url(reference))
                self.assertEqual(response.status_code, 404, response.content)
                self.assertNotIn(reference, response.content.decode())

    def test_an_old_sequential_reference_is_never_served(self):
        intent = self.started()
        CollectionIntent.objects.filter(pk=intent.pk).update(reference="CXP-1260101001")
        self.assertEqual(self.anonymous.get(self.url("CXP-1260101001")).status_code, 404)

    def test_the_payload_is_the_five_neutral_facts_and_no_one_elses_details(self):
        intent = self.started()

        response = self.anonymous.get(self.url(intent.reference))

        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertEqual(set(data), PAYLOAD)
        self.assertEqual((data["status"], data["amount"], data["currency"], data["paid_at"]),
                         ("PENDING", 50_000, "NGN", None))
        self.assertEqual(data["payee_name"], self.school.name)
        body = response.content.decode().lower()
        for private in ("adeyemi", "tunde", "mum@", "lekki", self.adeyemi.code,
                        "narration", "branch", "invoice"):
            self.assertNotIn(private.lower(), body)

    def test_a_payment_that_is_still_pending_is_confirmed_by_asking_the_provider(self):
        intent = self.started()
        self.fake.forced_status[intent.reference] = CollectionStatus.SUCCEEDED
        self.fake.forced_amount[intent.reference] = intent.amount

        response = self.anonymous.get(self.url(intent.reference))

        self.assertEqual(response.data["data"]["status"], "CONFIRMED")
        self.assertIsNotNone(response.data["data"]["paid_at"])
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)

    def test_a_payment_younger_than_ten_seconds_is_not_asked_about(self):
        intent = self.started(age_seconds=2)
        self.fake.forced_status[intent.reference] = CollectionStatus.SUCCEEDED
        with mock.patch.object(services, "confirm_collection") as confirm:
            response = self.anonymous.get(self.url(intent.reference))
        confirm.assert_not_called()
        self.assertEqual(response.data["data"]["status"], "PENDING")

    def test_the_provider_is_asked_once_per_interval_however_often_the_page_polls(self):
        intent = self.started()
        with mock.patch.object(services, "confirm_collection", side_effect=lambda i: i) as confirm:
            for _ in range(5):
                self.anonymous.get(self.url(intent.reference))
        self.assertEqual(confirm.call_count, 1)

    def test_a_provider_that_cannot_be_reached_leaves_the_payment_pending(self):
        from .exceptions import ProviderError

        intent = self.started()
        with mock.patch.object(services, "confirm_collection", side_effect=ProviderError("down")):
            response = self.anonymous.get(self.url(intent.reference))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "PENDING")

    def test_failed_and_abandoned_use_plain_words(self):
        for stored, word in ((CollectionStatus.FAILED, "FAILED"),
                             (CollectionStatus.ABANDONED, "ABANDONED")):
            with self.subTest(stored=stored):
                intent = self.started()
                CollectionIntent.objects.filter(pk=intent.pk).update(status=stored)
                self.assertEqual(self.anonymous.get(self.url(intent.reference)).data["data"]["status"], word)

    def test_a_one_branch_school_answers_the_same_way(self):
        intent = self.started(self.solo_books, self.solo_parent)
        data = self.anonymous.get(self.url(intent.reference)).data["data"]
        self.assertEqual((set(data), data["payee_name"]), (PAYLOAD, self.solo_school.name))

    def test_the_platforms_own_invoice_names_the_platform_as_payee(self):
        customer = Customer.objects.create(
            entity=self.platform, code="CSCH", name="Corona Group",
            receivable_account=Account.objects.get(entity=self.platform, code="1200"), branch=self.lagos)
        intent = self.started(self.platform, customer)
        data = self.anonymous.get(self.url(intent.reference)).data["data"]
        self.assertEqual(data["payee_name"], settings.PLATFORM_ISSUER["name"])

    def test_the_payee_name_matches_the_name_printed_on_the_invoice(self):
        from vs_finance.documents import _issuer_block, issuer_name

        for entity in (self.books, self.platform):
            with self.subTest(entity=entity.code):
                self.assertEqual(issuer_name(entity), _issuer_block(entity)["name"])

    def test_one_reference_has_a_budget_of_its_own(self):
        first, second = self.started(), self.started()
        with mock.patch.object(PublicCollectionReferenceThrottle, "THROTTLE_RATES",
                               {"payments_public_status_reference": "3/minute"}):
            statuses = [self.anonymous.get(self.url(first.reference)).status_code for _ in range(4)]
            self.assertEqual(statuses, [200, 200, 200, 429])
            self.assertEqual(self.anonymous.get(self.url(second.reference)).status_code, 200)

    def test_it_needs_no_sign_in(self):
        intent = self.started()
        self.assertEqual(self.anonymous.get(self.url(intent.reference)).status_code, 200)
