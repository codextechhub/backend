"""Paystack provider — collections + payouts + webhooks.  # Concrete adapter for the Paystack PSP.

Reference: https://paystack.com/docs/api/ . Base URL ``https://api.paystack.co``; every
call authenticates with ``Authorization: Bearer <secret_key>``. Amounts are in **kobo**
already (Paystack's NGN minor unit), so no conversion. Webhooks are signed with
``x-paystack-signature`` = HMAC-SHA512 of the raw request body using the same secret key.  # Use the raw body for signature verification.

All network I/O goes through :func:`vs_payments.providers.http.request_json`, which tests
patch — so this client is fully exercised without ever calling Paystack.  # Keep HTTP interactions centralized and testable.
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

# Paystack transaction/transfer status string → our neutral status.  # Translate provider states into our domain.
_COLLECTION_STATUS = {
    "success": "SUCCEEDED",
    "failed": "FAILED",
    "abandoned": "ABANDONED",
    "reversed": "REFUNDED",
}  # Mapping from Paystack collection status to neutral status.
_TRANSFER_STATUS = {
    "success": "PAID",
    "failed": "FAILED",
    "reversed": "REVERSED",
    "abandoned": "FAILED",
    "pending": "PROCESSING",
    "otp": "PROCESSING",
    "processing": "PROCESSING",
}  # Mapping from Paystack transfer status to neutral status.


# Group behavior for Paystack Provider.
class PaystackProvider(Provider):
    name = "PAYSTACK"  # Provider lookup key used by the registry.

    def __init__(self, *, secret_key: str, base_url: str = "https://api.paystack.co"):
        self.secret_key = secret_key  # Bearer token used for all API requests.
        self.base_url = base_url.rstrip("/")  # Normalize away a trailing slash once.

    # -- internals ---------------------------------------------------------- #  # Shared request helpers below.
    # Support the headers workflow.
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret_key}"}  # Standard Paystack auth header.

    # Support the post workflow.
    def _post(self, path: str, body: dict) -> dict:
        return request_json("POST", f"{self.base_url}{path}", headers=self._headers(),  # Send an authenticated POST.
                            body=body, provider=self.name)  # Include provider name for better error context.

    # Support the get workflow.
    def _get(self, path: str) -> dict:
        return request_json("GET", f"{self.base_url}{path}", headers=self._headers(),  # Send an authenticated GET.
                            provider=self.name)  # Include provider name for better error context.

    @staticmethod
    # Support the require ok workflow.
    def _require_ok(resp: dict):
        if not resp.get("status", False):
            raise ProviderError(resp.get("message", "Paystack request failed."),
                                provider="PAYSTACK")  # Tag the error with the provider name.
        return resp.get("data", {})

    # -- collection --------------------------------------------------------- #  # Collection-side operations.
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None):
        data = self._require_ok(self._post("/transaction/initialize", {  # Start a hosted payment checkout.
            "email": customer_email or "customer@example.com",  # Paystack expects an email value.
            "amount": amount,  # Amount is already in kobo.
            "currency": currency,  # Forward the requested currency as-is.
            "reference": reference,  # Use our merchant reference for idempotency.
            "callback_url": callback_url,  # Return URL after checkout.
            "metadata": {**(metadata or {}), "narration": narration,  # Preserve caller metadata.
                         "customer_name": customer_name},  # Attach the display name for support.
        }))
        return CheckoutResult(
            reference=reference,  # Echo our merchant reference back to the caller.
            provider_reference=str(data.get("reference", reference)),
            checkout_url=data.get("authorization_url", ""),
            authorization_code=data.get("access_code", ""),
            status="PENDING",  # Hosted checkout is pending until verified.
            raw=data,  # Preserve the raw provider data.
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None):
        # Paystack requires a Customer first, then a dedicated account against it.  # Two-step account setup.
        first, _, last = (customer_name or "Customer").partition(" ")  # Split the display name into first/last names.
        customer = self._require_ok(self._post("/customer", {  # Create the upstream Paystack customer.
            "email": customer_email or f"{reference}@example.com",  # Fall back to a deterministic placeholder email.
            "first_name": first, "last_name": last or first,  # Use the available name parts.
        }))
        body = {"customer": customer.get("customer_code", "")}
        if bank_code:  # Only include preferred bank when the caller supplied one.
            body["preferred_bank"] = bank_code  # Ask Paystack to prefer that bank.
        data = self._require_ok(self._post("/dedicated_account", body))  # Request the dedicated account.
        acct = data.get("dedicated_account", data)
        bank = acct.get("bank", {}) if isinstance(acct.get("bank"), dict) else {}
        return VirtualAccountResult(
            account_number=acct.get("account_number", ""),
            bank_name=bank.get("name", ""),
            account_name=acct.get("account_name", customer_name),
            provider_reference=str(acct.get("id", "")),
            raw=data,  # Keep the raw response for audit/debugging.
        )

    def verify_collection(self, *, reference, provider_reference=""):
        data = self._require_ok(self._get(f"/transaction/verify/{reference}"))  # Ask Paystack for the final state.
        gateway = (data.get("status") or "").lower()
        return CollectionStatusResult(
            reference=reference,  # Merchant reference passed back for correlation.
            provider_reference=str(data.get("id", provider_reference)),
            status=_COLLECTION_STATUS.get(gateway, "PROCESSING"),
            amount=int(data.get("amount", 0) or 0),
            currency=data.get("currency", "NGN"),
            raw=data,  # Keep the raw response payload.
        )

    # -- payout ------------------------------------------------------------- #  # Payout-side operations.
    # Handle the create transfer workflow.
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        recipient = self._require_ok(self._post("/transferrecipient", {  # Create or resolve the transfer recipient.
            "type": "nuban", "name": account_name or "Beneficiary",  # Paystack expects a recipient type and name.
            "account_number": account_number, "bank_code": bank_code, "currency": currency,  # Bank details for the payee.
        }))
        recipient_code = recipient.get("recipient_code", "")
        data = self._require_ok(self._post("/transfer", {  # Initiate the actual bank transfer.
            "source": "balance", "amount": amount, "recipient": recipient_code,  # Pull from the wallet balance.
            "reason": narration or "Payout", "reference": reference, "currency": currency,  # Attach bookkeeping fields.
        }))
        status = (data.get("status") or "").lower()
        return TransferResult(
            reference=reference,  # Merchant reference for the transfer.
            provider_reference=data.get("transfer_code", ""),
            status=_TRANSFER_STATUS.get(status, "PROCESSING"),
            recipient_code=recipient_code,  # Save the recipient token for later verification.
            raw=data,  # Preserve the raw response.
        )

    def verify_transfer(self, *, reference, provider_reference=""):
        data = self._require_ok(self._get(f"/transfer/verify/{reference}"))  # Re-query the final transfer state.
        status = (data.get("status") or "").lower()
        return TransferResult(
            reference=reference,  # Merchant reference for the transfer.
            provider_reference=data.get("transfer_code", provider_reference),
            status=_TRANSFER_STATUS.get(status, "PROCESSING"),
            amount=int(data.get("amount") or 0),  # Paystack transfer verify returns the kobo amount in data.amount.
            failure_reason=data.get("message", "") if status in ("failed", "reversed") else "",
            raw=data,  # Keep the raw provider payload.
        )

    # -- webhooks ----------------------------------------------------------- #  # Webhook verification and parsing.
    # Handle the verify signature workflow.
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        sent = _header(headers, "x-paystack-signature")  # Read the signature supplied by Paystack.
        if not sent:  # Missing signatures are invalid.
            return False
        expected = hmac.new(self.secret_key.encode(), raw_body, hashlib.sha512).hexdigest()  # Compute the expected HMAC.
        return hmac.compare_digest(sent, expected)  # Compare in constant time.

    # Handle the parse webhook workflow.
    def parse_webhook(self, *, payload, raw_body, headers):
        event = payload.get("event", "")
        data = payload.get("data", {})
        if event.startswith("transfer"):  # Transfer events correspond to outbound payouts.
            gateway = (data.get("status") or "").lower()
            status = _TRANSFER_STATUS.get(gateway, "PROCESSING")
            if event == "transfer.success":  # Paystack emits a definitive success event.
                status = "PAID"  # Explicitly mark the payout as paid.
            elif event == "transfer.failed":  # Terminal failure event.
                status = "FAILED"  # Mark the transfer failed.
            elif event == "transfer.reversed":  # Terminal reversal event.
                status = "REVERSED"  # Mark the transfer reversed.
            direction = "PAYOUT"  # Route transfer events to the payout confirm path.
        else:  # All remaining events are treated as collection-side events.
            status = (
                "SUCCEEDED" if event == "charge.success"  # charge.success is the canonical successful collection event.
                else _COLLECTION_STATUS.get((data.get("status") or "").lower(), "PROCESSING")
            )
            direction = "COLLECTION"  # Route non-transfer events to the collection confirm path.
        reference = data.get("reference", "")
        return WebhookParseResult(
            event_type=event,  # Preserve the provider event name.
            direction=direction,  # COLLECTION or PAYOUT.
            reference=reference,  # Merchant reference for matching local records.
            provider_reference=str(data.get("id", "")),
            status=status,  # Neutral status for the downstream confirm flow.
            amount=int(data.get("amount", 0) or 0),
            currency=data.get("currency", "NGN"),
            dedupe_key=f"PAYSTACK:{event}:{reference or data.get('id', '')}",
            raw=payload,  # Keep the original normalized payload.
        )


# Support the header workflow.
def _header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup (WSGI/DRF may upper/lower-case keys)."""
    if not headers:  # Missing headers should behave like an empty mapping.
        return ""
    name = name.lower()  # Normalize the lookup key once.
    for key, value in headers.items():  # Walk through the received headers.
        if key.lower() == name:  # Match case-insensitively.
            return value  # Return the original header value.
    return ""  # No matching header was found.
