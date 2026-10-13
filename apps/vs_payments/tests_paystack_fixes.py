"""Paystack adapter facts: the bank a dedicated account asks for, and events that carry no id.

A dedicated virtual account names its bank by slug (``wema-bank``), never by the
numeric bank code transfers use. And two different ``dedicatedaccount.assign.success``
events, which name no reference, are two events, not one delivered twice.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.exceptions import ValidationError

from .models import WebhookEvent
from .providers import registry
from .providers.paystack import PaystackProvider
from .services import _preferred_bank_slug
from .webhooks import ingest_webhook

_HTTP = "vs_payments.providers.paystack.request_json"


def _answer(method, url, **kwargs):
    """A Paystack reply good enough for the customer and dedicated-account calls."""
    if url.endswith("/customer"):
        return {"status": True, "data": {"customer_code": "CUS_x"}}
    return {"status": True, "data": {
        "id": 9, "account_number": "9900112233", "account_name": "Parent",
        "bank": {"name": "Wema Bank"}}}


class PreferredBankTests(SimpleTestCase):
    def requested_bank(self, provider, **kwargs):
        with patch(_HTTP, side_effect=_answer) as http:
            provider.create_virtual_account(
                reference="CXP-1", customer_name="Ada Obi", customer_email="a@x.test", **kwargs)
        accounts = [call for call in http.call_args_list if call.args[1].endswith("/dedicated_account")]
        return accounts[0].kwargs["body"]["preferred_bank"]

    def test_a_live_key_defaults_to_the_documented_slug(self):
        self.assertEqual(self.requested_bank(PaystackProvider(secret_key="sk_live_x")), "wema-bank")

    def test_a_test_key_defaults_to_the_only_bank_it_can_use(self):
        self.assertEqual(self.requested_bank(PaystackProvider(secret_key="sk_test_x")), "test-bank")

    def test_the_deployments_slug_beats_the_default_and_the_callers_beats_both(self):
        provider = PaystackProvider(secret_key="sk_live_x", dedicated_account_bank="titan-paystack")
        self.assertEqual(self.requested_bank(provider), "titan-paystack")
        self.assertEqual(self.requested_bank(provider, preferred_bank="wema-bank"), "wema-bank")

    def test_a_numeric_bank_code_is_refused_rather_than_passed_on_as_a_slug(self):
        with self.assertRaises(ValidationError):
            _preferred_bank_slug("035")
        self.assertEqual(_preferred_bank_slug(" Wema-Bank "), "wema-bank")
        self.assertEqual(_preferred_bank_slug(""), "")


class EventsWithoutAReferenceTests(TestCase):
    """Each distinct id-less event is stored; a re-delivery of one is not stored twice."""

    @classmethod
    def setUpTestData(cls):
        cls.secret = "sk_test_dedupe"

    def setUp(self):
        registry.register("PAYSTACK", PaystackProvider(secret_key=self.secret))
        self.addCleanup(registry.unregister, "PAYSTACK")

    def deliver(self, event, data):
        body = json.dumps({"event": event, "data": data}).encode()
        signature = hmac.new(self.secret.encode(), body, hashlib.sha512).hexdigest()
        return ingest_webhook(
            provider="PAYSTACK", raw_body=body, headers={"x-paystack-signature": signature})

    def test_two_different_assignments_are_two_events(self):
        first = self.deliver("dedicatedaccount.assign.success",
                             {"customer": {"customer_code": "CUS_a"},
                              "dedicated_account": {"account_number": "9900000001"}})
        second = self.deliver("dedicatedaccount.assign.success",
                              {"customer": {"customer_code": "CUS_b"},
                               "dedicated_account": {"account_number": "9900000002"}})

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(
            WebhookEvent.objects.filter(event_type="dedicatedaccount.assign.success").count(), 2)
        self.assertNotIn(first.dedupe_key, ("", "PAYSTACK:dedicatedaccount.assign.success:"))
        self.assertFalse(second.dedupe_key.endswith(":"))

    def test_the_same_event_delivered_again_is_the_same_row(self):
        data = {"customer": {"customer_code": "CUS_a"},
                "dedicated_account": {"account_number": "9900000001"}}
        first = self.deliver("dedicatedaccount.assign.success", data)
        again = self.deliver("dedicatedaccount.assign.success", data)

        self.assertEqual(first.pk, again.pk)
        self.assertEqual(WebhookEvent.objects.count(), 1)

    def test_an_event_with_an_id_still_keys_on_the_id(self):
        event = self.deliver("customeridentification.success", {"id": 4411})
        self.assertEqual(event.dedupe_key, "PAYSTACK:customeridentification.success:4411")
