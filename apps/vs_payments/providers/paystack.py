"""Paystack provider — collections + payouts + webhooks.

Reference: https://paystack.com/docs/api/ . Base URL ``https://api.paystack.co``; every
call authenticates with ``Authorization: Bearer <secret_key>``. Amounts are in **kobo**
already (Paystack's NGN minor unit), so no conversion. Webhooks are signed with
``x-paystack-signature`` = HMAC-SHA512 of the raw request body using the same secret key.

All network I/O goes through :func:`vs_payments.providers.http.request_json`, which tests
patch — so this client is fully exercised without ever calling Paystack.
"""
from __future__ import annotations

import hashlib
import hmac

from ..exceptions import ProviderError
from .base import (
    CheckoutResult,
    CollectionStatusResult,
    Provider,
    TransferResult,
    VirtualAccountResult,
    WebhookParseResult,
)
from .http import request_json

# Paystack transaction/transfer status string → our neutral status.
_COLLECTION_STATUS = {
    "success": "SUCCEEDED",
    "failed": "FAILED",
    "abandoned": "ABANDONED",
    "reversed": "REFUNDED",
}
_TRANSFER_STATUS = {
    "success": "PAID",
    "failed": "FAILED",
    "reversed": "REVERSED",
    "abandoned": "FAILED",
    "pending": "PROCESSING",
    "otp": "PROCESSING",
    "processing": "PROCESSING",
}


class PaystackProvider(Provider):
    name = "PAYSTACK"

    def __init__(self, *, secret_key: str, base_url: str = "https://api.paystack.co"):
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")

    # -- internals ---------------------------------------------------------- #
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret_key}"}

    def _post(self, path: str, body: dict) -> dict:
        return request_json("POST", f"{self.base_url}{path}", headers=self._headers(),
                            body=body, provider=self.name)

    def _get(self, path: str) -> dict:
        return request_json("GET", f"{self.base_url}{path}", headers=self._headers(),
                            provider=self.name)

    @staticmethod
    def _require_ok(resp: dict):
        if not resp.get("status", False):
            raise ProviderError(resp.get("message", "Paystack request failed."),
                                provider="PAYSTACK")
        return resp.get("data", {})

    # -- collection --------------------------------------------------------- #
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None):
        data = self._require_ok(self._post("/transaction/initialize", {
            "email": customer_email or "customer@example.com",
            "amount": amount,
            "currency": currency,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": {**(metadata or {}), "narration": narration,
                         "customer_name": customer_name},
        }))
        return CheckoutResult(
            reference=reference,
            provider_reference=str(data.get("reference", reference)),
            checkout_url=data.get("authorization_url", ""),
            authorization_code=data.get("access_code", ""),
            status="PENDING",
            raw=data,
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None):
        # Paystack requires a Customer first, then a dedicated account against it.
        first, _, last = (customer_name or "Customer").partition(" ")
        customer = self._require_ok(self._post("/customer", {
            "email": customer_email or f"{reference}@example.com",
            "first_name": first, "last_name": last or first,
        }))
        body = {"customer": customer.get("customer_code", "")}
        if bank_code:
            body["preferred_bank"] = bank_code
        data = self._require_ok(self._post("/dedicated_account", body))
        acct = data.get("dedicated_account", data)
        bank = acct.get("bank", {}) if isinstance(acct.get("bank"), dict) else {}
        return VirtualAccountResult(
            account_number=acct.get("account_number", ""),
            bank_name=bank.get("name", ""),
            account_name=acct.get("account_name", customer_name),
            provider_reference=str(acct.get("id", "")),
            raw=data,
        )

    def verify_collection(self, *, reference, provider_reference=""):
        data = self._require_ok(self._get(f"/transaction/verify/{reference}"))
        gateway = (data.get("status") or "").lower()
        return CollectionStatusResult(
            reference=reference,
            provider_reference=str(data.get("id", provider_reference)),
            status=_COLLECTION_STATUS.get(gateway, "PROCESSING"),
            amount=int(data.get("amount", 0) or 0),
            currency=data.get("currency", "NGN"),
            raw=data,
        )

    # -- payout ------------------------------------------------------------- #
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        recipient = self._require_ok(self._post("/transferrecipient", {
            "type": "nuban", "name": account_name or "Beneficiary",
            "account_number": account_number, "bank_code": bank_code, "currency": currency,
        }))
        recipient_code = recipient.get("recipient_code", "")
        data = self._require_ok(self._post("/transfer", {
            "source": "balance", "amount": amount, "recipient": recipient_code,
            "reason": narration or "Payout", "reference": reference, "currency": currency,
        }))
        status = (data.get("status") or "").lower()
        return TransferResult(
            reference=reference,
            provider_reference=data.get("transfer_code", ""),
            status=_TRANSFER_STATUS.get(status, "PROCESSING"),
            recipient_code=recipient_code,
            raw=data,
        )

    def verify_transfer(self, *, reference, provider_reference=""):
        data = self._require_ok(self._get(f"/transfer/verify/{reference}"))
        status = (data.get("status") or "").lower()
        return TransferResult(
            reference=reference,
            provider_reference=data.get("transfer_code", provider_reference),
            status=_TRANSFER_STATUS.get(status, "PROCESSING"),
            failure_reason=data.get("message", "") if status in ("failed", "reversed") else "",
            raw=data,
        )

    # -- webhooks ----------------------------------------------------------- #
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        sent = _header(headers, "x-paystack-signature")
        if not sent:
            return False
        expected = hmac.new(self.secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(sent, expected)

    def parse_webhook(self, *, payload, raw_body, headers):
        event = payload.get("event", "")
        data = payload.get("data", {})
        if event.startswith("transfer"):
            gateway = (data.get("status") or "").lower()
            status = _TRANSFER_STATUS.get(gateway, "PROCESSING")
            if event == "transfer.success":
                status = "PAID"
            elif event == "transfer.failed":
                status = "FAILED"
            elif event == "transfer.reversed":
                status = "REVERSED"
            direction = "PAYOUT"
        else:
            status = "SUCCEEDED" if event == "charge.success" else \
                _COLLECTION_STATUS.get((data.get("status") or "").lower(), "PROCESSING")
            direction = "COLLECTION"
        reference = data.get("reference", "")
        return WebhookParseResult(
            event_type=event, direction=direction, reference=reference,
            provider_reference=str(data.get("id", "")), status=status,
            amount=int(data.get("amount", 0) or 0), currency=data.get("currency", "NGN"),
            dedupe_key=f"PAYSTACK:{event}:{reference or data.get('id', '')}",
            raw=payload,
        )


def _header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup (WSGI/DRF may upper/lower-case keys)."""
    if not headers:
        return ""
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""
