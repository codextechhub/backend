"""A provider refusing a feature the merchant has not been given is not an outage.

Paystack answers ``POST /dedicated_account`` with HTTP 403 and code
``feature_unavailable`` for a business that has not been approved for dedicated
accounts. Asking again changes nothing until Paystack turns the feature on, so
the adapter raises :class:`ProviderFeatureUnavailableError`, which callers treat
as "not set up". Every other refusal or failure stays a plain ``ProviderError``.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

from django.test import SimpleTestCase

from .exceptions import ProviderError, ProviderFeatureUnavailableError, ProviderNotConfiguredError
from .providers.http import request_json
from .providers.paystack import PaystackProvider

_HTTP = "vs_payments.providers.paystack.request_json"

_FEATURE_UNAVAILABLE = {
    "status": False,
    "message": "Dedicated NUBAN is not available for your business",
    "meta": {"nextStep": "You can send us an email at support@paystack.com to make a request for the service"},
    "type": "api_error",
    "code": "feature_unavailable",
}


def _http_error(status, body):
    raw = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    return urllib.error.HTTPError("https://api.paystack.co/x", status, "refused", {}, io.BytesIO(raw))


def _refusal(status, body):
    """A ``request_json`` stand-in: the customer is created, the account call refused."""
    def answer(method, url, **kwargs):
        if url.endswith("/customer"):
            return {"status": True, "data": {"customer_code": "CUS_x"}}
        exc = ProviderError(f"PAYSTACK returned HTTP {status}", provider="PAYSTACK", provider_code=str(status))
        exc.response_body = body
        raise exc
    return answer


class ResponseBodyTests(SimpleTestCase):
    """The shared HTTP helper keeps the provider's refusal for its adapter to read."""

    def test_a_json_refusal_is_kept_on_the_error(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(403, _FEATURE_UNAVAILABLE)):
            with self.assertRaises(ProviderError) as caught:
                request_json("POST", "https://api.paystack.co/dedicated_account", provider="PAYSTACK")
        self.assertEqual(caught.exception.provider_code, "403")
        self.assertEqual(caught.exception.response_body["code"], "feature_unavailable")

    def test_a_refusal_that_is_not_json_keeps_none(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(502, "<html>Bad gateway</html>")):
            with self.assertRaises(ProviderError) as caught:
                request_json("GET", "https://api.paystack.co/balance", provider="PAYSTACK")
        self.assertIsNone(caught.exception.response_body)

    def test_the_body_is_not_sent_on_to_api_callers(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(403, _FEATURE_UNAVAILABLE)):
            with self.assertRaises(ProviderError) as caught:
                request_json("POST", "https://api.paystack.co/dedicated_account", provider="PAYSTACK")
        self.assertNotIn("response_body", caught.exception.extra)


class PaystackFeatureUnavailableTests(SimpleTestCase):
    """Only Paystack's ``feature_unavailable`` refusal reads as not set up."""

    def open_account(self, status, body):
        provider = PaystackProvider(secret_key="sk_test_x")
        with patch(_HTTP, side_effect=_refusal(status, body)):
            provider.create_virtual_account(
                reference="CXP-1", customer_name="Tunde Adeleke", customer_email="t@x.test")

    def test_a_business_without_dedicated_accounts_is_not_set_up(self):
        with self.assertRaises(ProviderFeatureUnavailableError) as caught:
            self.open_account(403, _FEATURE_UNAVAILABLE)
        error = caught.exception
        self.assertIsInstance(error, ProviderNotConfiguredError)
        self.assertIsInstance(error, ProviderError)
        self.assertIn("Dedicated NUBAN is not available", error.message)
        self.assertEqual(error.provider_code, "403")

    def test_another_403_is_still_a_provider_error(self):
        body = {"status": False, "message": "Invalid key", "code": "invalid_key"}
        with self.assertRaises(ProviderError) as caught:
            self.open_account(403, body)
        self.assertNotIsInstance(caught.exception, ProviderNotConfiguredError)

    def test_a_server_error_is_still_a_provider_error(self):
        with self.assertRaises(ProviderError) as caught:
            self.open_account(503, None)
        self.assertNotIsInstance(caught.exception, ProviderNotConfiguredError)
