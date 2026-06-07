"""OPay provider — collections + payouts + webhooks.

Reference: https://documentation.opaycheckout.com/ . OPay's cashier APIs authenticate with
two keys issued on the merchant dashboard: the **secret key** signs create/transfer
requests (``Authorization: Bearer <HMAC-SHA512(payload, secret)>``) and the **public key**
is the bearer for status queries; every call also carries a ``MerchantId`` header.

OPay issues environment-specific hosts and endpoint paths per merchant, so the base URL
and paths are injected (from settings) rather than hard-coded — confirm them against your
dashboard. Where a path isn't configured, the relevant method raises a clear
``ProviderError`` instead of guessing. Network I/O routes through
:func:`vs_payments.providers.http.request_json` (patched in tests).

NOTE: OPay's exact request/response field names vary by product (Cashier vs. Transaction
vs. Transfer). The mappings below follow the documented Cashier shape and read defensively;
verify field names against your onboarding docs before going live.
"""
from __future__ import annotations

import hashlib
import hmac
import json

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

# OPay status string → our neutral status.
_COLLECTION_STATUS = {
    "SUCCESS": "SUCCEEDED",
    "FAIL": "FAILED",
    "FAILED": "FAILED",
    "CLOSE": "ABANDONED",
    "INITIAL": "PROCESSING",
    "PENDING": "PROCESSING",
}
_TRANSFER_STATUS = {
    "SUCCESS": "PAID",
    "SUCCESSFUL": "PAID",
    "FAIL": "FAILED",
    "FAILED": "FAILED",
    "INITIAL": "PROCESSING",
    "PENDING": "PROCESSING",
    "PROCESSING": "PROCESSING",
}


class OPayProvider(Provider):
    name = "OPAY"

    def __init__(self, *, merchant_id, secret_key, public_key="",
                 base_url="https://api.opaycheckout.com", create_path="", status_path="",
                 transfer_path="", transfer_status_path="", country="NG"):
        self.merchant_id = merchant_id
        self.secret_key = secret_key
        self.public_key = public_key
        self.base_url = base_url.rstrip("/")
        self.create_path = create_path
        self.status_path = status_path
        self.transfer_path = transfer_path
        self.transfer_status_path = transfer_status_path
        self.country = country

    # -- internals ---------------------------------------------------------- #
    def sign(self, body: dict) -> str:
        """HMAC-SHA512 of the JSON payload (keys sorted) using the secret key."""
        serialized = json.dumps(body, separators=(",", ":"), sort_keys=True)
        return hmac.new(self.secret_key.encode(), serialized.encode(), hashlib.sha512).hexdigest()

    def _signed_post(self, path: str, body: dict) -> dict:
        if not path:
            raise ProviderError("OPay endpoint path is not configured.", provider=self.name)
        headers = {"Authorization": f"Bearer {self.sign(body)}", "MerchantId": self.merchant_id}
        return request_json("POST", f"{self.base_url}{path}", headers=headers,
                            body=body, provider=self.name)

    def _public_post(self, path: str, body: dict) -> dict:
        if not path:
            raise ProviderError("OPay endpoint path is not configured.", provider=self.name)
        headers = {"Authorization": f"Bearer {self.public_key}", "MerchantId": self.merchant_id}
        return request_json("POST", f"{self.base_url}{path}", headers=headers,
                            body=body, provider=self.name)

    @staticmethod
    def _data(resp: dict) -> dict:
        # OPay wraps results as {"code": "00000", "message": ..., "data": {...}}.
        code = str(resp.get("code", ""))
        if code and code not in ("00000", "0", "SUCCESS"):
            raise ProviderError(resp.get("message", "OPay request failed."),
                                provider="OPAY", provider_code=code)
        return resp.get("data", resp) or {}

    # -- collection --------------------------------------------------------- #
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None):
        data = self._data(self._signed_post(self.create_path, {
            "country": self.country,
            "reference": reference,
            "amount": {"total": amount, "currency": currency},
            "returnUrl": callback_url,
            "callbackUrl": callback_url,
            "expireAt": 30,
            "userInfo": {"userEmail": customer_email, "userName": customer_name},
            "productName": narration or "Payment",
            "productDesc": narration or "Payment",
        }))
        return CheckoutResult(
            reference=reference,
            provider_reference=str(data.get("orderNo", "")),
            checkout_url=data.get("cashierUrl", data.get("url", "")),
            status="PENDING",
            raw=data,
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None):
        # OPay virtual/static account provisioning is a distinct product API; require it
        # to be explicitly enabled rather than guessing an endpoint.
        raise ProviderError(
            "OPay virtual-account provisioning is not configured for this merchant; "
            "use checkout, or wire the dedicated OPay VA endpoint.",
            provider=self.name,
        )

    def verify_collection(self, *, reference, provider_reference=""):
        data = self._data(self._public_post(self.status_path, {
            "country": self.country, "reference": reference,
        }))
        gateway = str(data.get("status", "")).upper()
        return CollectionStatusResult(
            reference=reference,
            provider_reference=str(data.get("orderNo", provider_reference)),
            status=_COLLECTION_STATUS.get(gateway, "PROCESSING"),
            amount=int((data.get("amount") or {}).get("total", 0) or 0),
            currency=(data.get("amount") or {}).get("currency", "NGN"),
            raw=data,
        )

    # -- payout ------------------------------------------------------------- #
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        data = self._data(self._signed_post(self.transfer_path, {
            "country": self.country,
            "reference": reference,
            "amount": {"total": amount, "currency": currency},
            "receiver": {
                "bankCode": bank_code, "bankAccountNumber": account_number,
                "name": account_name,
            },
            "reason": narration or "Payout",
        }))
        gateway = str(data.get("status", "")).upper()
        return TransferResult(
            reference=reference,
            provider_reference=str(data.get("orderNo", "")),
            status=_TRANSFER_STATUS.get(gateway, "PROCESSING"),
            raw=data,
        )

    def verify_transfer(self, *, reference, provider_reference=""):
        data = self._data(self._public_post(self.transfer_status_path, {
            "country": self.country, "reference": reference,
        }))
        gateway = str(data.get("status", "")).upper()
        return TransferResult(
            reference=reference,
            provider_reference=str(data.get("orderNo", provider_reference)),
            status=_TRANSFER_STATUS.get(gateway, "PROCESSING"),
            failure_reason=data.get("failureReason", "") if gateway in ("FAIL", "FAILED") else "",
            raw=data,
        )

    # -- webhooks ----------------------------------------------------------- #
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            return False
        sent = payload.get("sha512", "") or _header(headers, "Authorization").replace("Bearer ", "")
        if not sent:
            return False
        # OPay signs the inner payload object (keys sorted) with the secret key.
        inner = payload.get("payload", payload)
        expected = self.sign(inner) if isinstance(inner, dict) else ""
        return bool(expected) and hmac.compare_digest(sent, expected)

    def parse_webhook(self, *, payload, raw_body, headers):
        body = payload.get("payload", payload)
        gateway = str(body.get("status", "")).upper()
        # Heuristic: a transfer event carries a transferStatus / instrumentType.
        is_payout = "transfer" in str(payload.get("type", "")).lower() or "transferStatus" in body
        if is_payout:
            status = _TRANSFER_STATUS.get(gateway, "PROCESSING")
            direction = "PAYOUT"
        else:
            status = _COLLECTION_STATUS.get(gateway, "PROCESSING")
            direction = "COLLECTION"
        reference = body.get("reference", "")
        return WebhookParseResult(
            event_type=str(payload.get("type", body.get("status", ""))),
            direction=direction, reference=reference,
            provider_reference=str(body.get("orderNo", "")), status=status,
            amount=int((body.get("amount") or {}).get("total", 0) or 0),
            currency=(body.get("amount") or {}).get("currency", "NGN"),
            dedupe_key=f"OPAY:{body.get('orderNo', '')}:{reference}:{gateway}",
            raw=payload,
        )


def _header(headers: dict, name: str) -> str:
    if not headers:
        return ""
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""
