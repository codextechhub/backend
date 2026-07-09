"""OPay provider — collections + payouts + webhooks.  # Concrete adapter for the OPay PSP.

Reference: https://documentation.opaycheckout.com/ . OPay's cashier APIs authenticate with
two keys issued on the merchant dashboard: the **secret key** signs create/transfer
requests (``Authorization: Bearer <HMAC-SHA512(payload, secret)>``) and the **public key**
is the bearer for status queries; every call also carries a ``MerchantId`` header.

OPay issues environment-specific hosts and endpoint paths per merchant, so the base URL
and paths are injected (from settings) rather than hard-coded — confirm them against your
dashboard. Where a path isn't configured, the relevant method raises a clear
``ProviderError`` instead of guessing. Network I/O routes through
:func:`vs_payments.providers.http.request_json` (patched in tests).  # Keep configuration explicit and testable.

NOTE: OPay's exact request/response field names vary by product (Cashier vs. Transaction
vs. Transfer). The mappings below follow the documented Cashier shape and read defensively;
verify field names against your onboarding docs before going live.  # Treat the adapter as defensive, not magical.
"""
from __future__ import annotations  # Defer annotation evaluation for forward references.

import hashlib  # Used to compute webhook HMAC signatures.
import hmac  # Used to compare signatures securely.
import json  # Used to canonicalize payloads before signing.

from ..exceptions import ProviderError  # Raised when the upstream provider returns an error.
from .base import (
    CheckoutResult,  # Neutral checkout result.
    CollectionStatusResult,  # Neutral collection verification result.
    Provider,  # Combined provider contract.
    TransferResult,  # Neutral payout result.
    VirtualAccountResult,  # Neutral virtual account result.
    WebhookParseResult,  # Neutral parsed webhook event.
)
from .http import request_json  # Shared HTTP helper used by all providers.

# OPay status string → our neutral status.  # Translate provider lifecycle states into our own vocabulary.
_COLLECTION_STATUS = {
    "SUCCESS": "SUCCEEDED",
    "FAIL": "FAILED",
    "FAILED": "FAILED",
    "CLOSE": "ABANDONED",
    "INITIAL": "PROCESSING",
    "PENDING": "PROCESSING",
}  # Collection status translation table.
_TRANSFER_STATUS = {
    "SUCCESS": "PAID",
    "SUCCESSFUL": "PAID",
    "FAIL": "FAILED",
    "FAILED": "FAILED",
    "INITIAL": "PROCESSING",
    "PENDING": "PROCESSING",
    "PROCESSING": "PROCESSING",
}  # Transfer status translation table.


class OPayProvider(Provider):
    name = "OPAY"  # Registry key for this provider.

    def __init__(self, *, merchant_id, secret_key, public_key="",
                 base_url="https://api.opaycheckout.com", create_path="", status_path="",
                 transfer_path="", transfer_status_path="", country="NG"):
        self.merchant_id = merchant_id  # Merchant dashboard identifier.
        self.secret_key = secret_key  # Secret key used to sign write operations.
        self.public_key = public_key  # Public key used for status queries.
        self.base_url = base_url.rstrip("/")  # Normalize the API host once.
        self.create_path = create_path  # Hosted checkout/create endpoint.
        self.status_path = status_path  # Collection status endpoint.
        self.transfer_path = transfer_path  # Transfer creation endpoint.
        self.transfer_status_path = transfer_status_path  # Transfer status endpoint.
        self.country = country  # Default settlement country code.

    # -- internals ---------------------------------------------------------- #  # Shared request helpers.
    def sign(self, body: dict) -> str:
        """HMAC-SHA512 of the JSON payload (keys sorted) using the secret key."""
        serialized = json.dumps(body, separators=(",", ":"), sort_keys=True)  # Canonicalize the JSON payload before signing.
        return hmac.new(self.secret_key.encode(), serialized.encode(), hashlib.sha512).hexdigest()  # Compute the HMAC signature.

    def _signed_post(self, path: str, body: dict) -> dict:
        if not path:  # Missing endpoint paths should fail fast.
            raise ProviderError("OPay endpoint path is not configured.", provider=self.name)
        headers = {"Authorization": f"Bearer {self.sign(body)}", "MerchantId": self.merchant_id}  # Signed auth headers.
        return request_json("POST", f"{self.base_url}{path}", headers=headers,  # Send the authenticated request.
                            body=body, provider=self.name)

    def _public_post(self, path: str, body: dict) -> dict:
        if not path:  # Missing endpoint paths should fail fast.
            raise ProviderError("OPay endpoint path is not configured.", provider=self.name)
        headers = {"Authorization": f"Bearer {self.public_key}", "MerchantId": self.merchant_id}  # Status queries use the public key.
        return request_json("POST", f"{self.base_url}{path}", headers=headers,  # Send the unsigned public request.
                            body=body, provider=self.name)

    @staticmethod
    def _data(resp: dict) -> dict:
        # OPay wraps results as {"code": "00000", "message": ..., "data": {...}}.  # Normalize that shape here.
        code = str(resp.get("code", ""))  # Read the provider status code as a string.
        if code and code not in ("00000", "0", "SUCCESS"):  # Treat any non-success code as a provider error.
            raise ProviderError(resp.get("message", "OPay request failed."),  # Surface the provider message.
                                provider="OPAY", provider_code=code)  # Attach provider and code for debugging.
        return resp.get("data", resp) or {}  # Return the nested payload or the response itself.

    # -- collection --------------------------------------------------------- #  # Collection-side methods.
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None):
        data = self._data(self._signed_post(self.create_path, {  # Create a hosted cashier session.
            "country": self.country,  # Provide the settlement country.
            "reference": reference,  # Merchant reference for reconciliation.
            "amount": {"total": amount, "currency": currency},  # OPay expects a nested amount object.
            "returnUrl": callback_url,  # Browser return URL after checkout.
            "callbackUrl": callback_url,  # Server callback URL after payment.
            "expireAt": 30,  # Short-lived checkout window.
            "userInfo": {"userEmail": customer_email, "userName": customer_name},  # Customer identity fields.
            "productName": narration or "Payment",  # Human-readable product label.
            "productDesc": narration or "Payment",  # Human-readable description.
        }))
        return CheckoutResult(
            reference=reference,  # Echo our merchant reference.
            provider_reference=str(data.get("orderNo", "")),  # Store OPay's order number.
            checkout_url=data.get("cashierUrl", data.get("url", "")),  # Hosted checkout URL if one is returned.
            status="PENDING",  # Hosted checkout starts as pending.
            raw=data,  # Keep the raw response payload.
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None):
        # OPay virtual/static account provisioning is a distinct product API.  # This merchant has not wired it here.
        raise ProviderError(
            "OPay virtual-account provisioning is not configured for this merchant; "
            "use checkout, or wire the dedicated OPay VA endpoint.",
            provider=self.name,
        )

    def verify_collection(self, *, reference, provider_reference=""):
        data = self._data(self._public_post(self.status_path, {  # Query the payment status endpoint.
            "country": self.country, "reference": reference,  # Use the merchant reference for lookup.
        }))
        gateway = str(data.get("status", "")).upper()  # Normalize the returned status.
        return CollectionStatusResult(
            reference=reference,  # Merchant reference for the collection.
            provider_reference=str(data.get("orderNo", provider_reference)),  # Use OPay's order number when available.
            status=_COLLECTION_STATUS.get(gateway, "PROCESSING"),  # Map provider state to our neutral state.
            amount=int((data.get("amount") or {}).get("total", 0) or 0),  # Amount field is nested under amount.total.
            currency=(data.get("amount") or {}).get("currency", "NGN"),  # Currency is also nested.
            raw=data,  # Preserve the raw response.
        )

    # -- payout ------------------------------------------------------------- #  # Payout-side methods.
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        data = self._data(self._signed_post(self.transfer_path, {  # Initiate an OPay transfer.
            "country": self.country,  # Settlement country.
            "reference": reference,  # Merchant reference for reconciliation.
            "amount": {"total": amount, "currency": currency},  # OPay expects a nested amount object.
            "receiver": {  # Receiver bank details.
                "bankCode": bank_code, "bankAccountNumber": account_number,
                "name": account_name,
            },
            "reason": narration or "Payout",  # Human-readable transfer reason.
        }))
        gateway = str(data.get("status", "")).upper()  # Normalize the transfer status.
        return TransferResult(
            reference=reference,  # Merchant payout reference.
            provider_reference=str(data.get("orderNo", "")),  # OPay order number.
            status=_TRANSFER_STATUS.get(gateway, "PROCESSING"),  # Map provider state to our neutral state.
            raw=data,  # Preserve the provider payload.
        )

    def verify_transfer(self, *, reference, provider_reference=""):
        data = self._data(self._public_post(self.transfer_status_path, {  # Query the transfer status endpoint.
            "country": self.country, "reference": reference,  # Use the merchant reference for lookup.
        }))
        gateway = str(data.get("status", "")).upper()  # Normalize the returned transfer status.
        return TransferResult(
            reference=reference,  # Merchant payout reference.
            provider_reference=str(data.get("orderNo", provider_reference)),  # Prefer OPay's order number.
            status=_TRANSFER_STATUS.get(gateway, "PROCESSING"),  # Map provider state to neutral state.
            failure_reason=data.get("failureReason", "") if gateway in ("FAIL", "FAILED") else "",  # Keep failure details only on terminal failure.
            raw=data,  # Preserve the raw payload.
        )

    # -- webhooks ----------------------------------------------------------- #  # Webhook verification and parsing.
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        try:  # The signature is embedded in the JSON wrapper for many OPay webhook shapes.
            payload = json.loads(raw_body or b"{}")  # Parse the raw body so we can inspect the wrapper fields.
        except json.JSONDecodeError:  # Invalid JSON cannot be trusted.
            return False
        sent = payload.get("sha512", "") or _header(headers, "Authorization").replace("Bearer ", "")  # Accept either body or header signature.
        if not sent:  # Missing signature means the event is not trustworthy.
            return False
        # OPay signs the inner payload object (keys sorted) with the secret key.  # Canonicalize before signing.
        inner = payload.get("payload", payload)  # Some webhooks wrap the signed payload inside "payload".
        expected = self.sign(inner) if isinstance(inner, dict) else ""  # Compute the expected HMAC signature.
        return bool(expected) and hmac.compare_digest(sent, expected)  # Compare signatures in constant time.

    def parse_webhook(self, *, payload, raw_body, headers):
        body = payload.get("payload", payload)  # Some webhook formats wrap the inner payload.
        gateway = str(body.get("status", "")).upper()  # Normalize the provider status field.
        # Heuristic: a transfer event carries a transferStatus / instrumentType.  # Distinguish money-out from money-in.
        is_payout = "transfer" in str(payload.get("type", "")).lower() or "transferStatus" in body
        if is_payout:  # Outbound transfer webhook.
            status = _TRANSFER_STATUS.get(gateway, "PROCESSING")  # Map to neutral payout state.
            direction = "PAYOUT"  # Route to payout confirmation.
        else:  # Inbound collection webhook.
            status = _COLLECTION_STATUS.get(gateway, "PROCESSING")  # Map to neutral collection state.
            direction = "COLLECTION"  # Route to collection confirmation.
        reference = body.get("reference", "")  # Merchant reference when OPay echoes it back.
        return WebhookParseResult(
            event_type=str(payload.get("type", body.get("status", ""))),  # Preserve the provider event type.
            direction=direction, reference=reference,  # Route and correlate the event.
            provider_reference=str(body.get("orderNo", "")),  # OPay order number.
            status=status,  # Neutral status for downstream processing.
            amount=int((body.get("amount") or {}).get("total", 0) or 0),  # Nested total amount in kobo.
            currency=(body.get("amount") or {}).get("currency", "NGN"),  # Nested currency code.
            dedupe_key=f"OPAY:{body.get('orderNo', '')}:{reference}:{gateway}",  # Stable idempotency key.
            raw=payload,  # Preserve the normalized payload.
        )


def _header(headers: dict, name: str) -> str:
    if not headers:  # Empty headers should behave like no match.
        return ""
    name = name.lower()  # Normalize the lookup key once.
    for key, value in headers.items():  # Walk through all header keys.
        if key.lower() == name:  # Match case-insensitively.
            return value  # Return the header value.
    return ""  # No header matched the requested name.
