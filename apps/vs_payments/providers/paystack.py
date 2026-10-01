"""Paystack provider - collections + payouts + webhooks.  # Concrete adapter for the Paystack PSP.

Reference: https://paystack.com/docs/api/ . Base URL ``https://api.paystack.co``; every
call authenticates with ``Authorization: Bearer <secret_key>``. Amounts are in **kobo**
already (Paystack's NGN minor unit), so no conversion. Webhooks are signed with
``x-paystack-signature`` = HMAC-SHA512 of the raw request body using the same secret key.  # Use the raw body for signature verification.

All network I/O goes through :func:`vs_payments.providers.http.request_json`, which tests
patch - so this client is fully exercised without ever calling Paystack.  # Keep HTTP interactions centralized and testable.

Paystack facts this adapter relies on for settlement subaccounts and fees, each to
confirm against Paystack's documentation (or its account manager) before a tenant
takes payments directly:

* ``POST /transaction/initialize`` accepts ``subaccount`` (a subaccount code) and
  ``bearer``, and ``bearer: "subaccount"`` makes the subaccount bear Paystack's fee,
  so the branch receives the payment less the fee and the main account keeps none.
* ``POST /dedicated_account`` accepts ``subaccount``, so a dedicated virtual account's
  deposits settle to that subaccount's bank account; whether ``bearer`` is honoured
  there too is to be confirmed (it is sent).
* ``GET /transaction/verify/<reference>`` reports the fee Paystack kept, in kobo, as
  ``data.fees``.
* ``POST /subaccount`` takes ``business_name``, ``settlement_bank`` (the bank's
  code), ``account_number`` and ``percentage_charge`` (the main account's share of
  each payment, 0 for none), and answers ``data.subaccount_code``; the resolved
  holder name is read from ``data.account_name`` when present.
* ``PUT /subaccount/<code>`` takes the same fields to point an existing subaccount
  at a new bank account.
* A subaccount settles to its bank on Paystack's own schedule, usually the next
  working day, as one amount per settlement for the payments it covers.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac

from ..exceptions import ProviderError
from .base import (
    CheckoutResult,
    CollectionStatusResult,
    Provider,
    SubaccountResult,
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

    def _put(self, path: str, body: dict) -> dict:
        return request_json("PUT", f"{self.base_url}{path}", headers=self._headers(),
                            body=body, provider=self.name)

    @staticmethod
    # Support the require ok workflow.
    def _require_ok(resp: dict):
        if not resp.get("status", False):
            raise ProviderError(resp.get("message", "Paystack request failed."),
                                provider="PAYSTACK")  # Tag the error with the provider name.
        return resp.get("data", {})

    def healthcheck(self) -> bool:
        # Balance is authenticated and read-only. Do not return its financial data.
        self._require_ok(self._get("/balance"))
        return True

    # -- collection --------------------------------------------------------- #  # Collection-side operations.
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None,
                        subaccount=""):
        body = {
            "email": customer_email or "customer@example.com",  # Paystack expects an email value.
            "amount": amount,  # Amount is already in kobo.
            "currency": currency,  # Forward the requested currency as-is.
            "reference": reference,  # Use our merchant reference for idempotency.
            "callback_url": callback_url,  # Return URL after checkout.
            "metadata": {**(metadata or {}), "narration": narration,  # Preserve caller metadata.
                         "customer_name": customer_name},  # Attach the display name for support.
        }
        if subaccount:  # Settle to the branch's bank, the branch bearing the fee.
            body["subaccount"] = subaccount
            body["bearer"] = "subaccount"
        data = self._require_ok(self._post("/transaction/initialize", body))  # Start a hosted payment checkout.
        return CheckoutResult(
            reference=reference,  # Echo our merchant reference back to the caller.
            provider_reference=str(data.get("reference", reference)),
            checkout_url=data.get("authorization_url", ""),
            authorization_code=data.get("access_code", ""),
            status="PENDING",  # Hosted checkout is pending until verified.
            raw=data,  # Preserve the raw provider data.
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None, subaccount=""):
        # Paystack requires a Customer first, then a dedicated account against it.  # Two-step account setup.
        first, _, last = (customer_name or "Customer").partition(" ")  # Split the display name into first/last names.
        customer = self._require_ok(self._post("/customer", {  # Create the upstream Paystack customer.
            "email": customer_email or f"{reference}@example.com",  # Fall back to a deterministic placeholder email.
            "first_name": first, "last_name": last or first,  # Use the available name parts.
        }))
        body = {"customer": customer.get("customer_code", "")}
        if bank_code:  # Only include preferred bank when the caller supplied one.
            body["preferred_bank"] = bank_code  # Ask Paystack to prefer that bank.
        if subaccount:  # Deposits settle to the branch's bank, the branch bearing the fee.
            body["subaccount"] = subaccount
            body["bearer"] = "subaccount"
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
            paid_at=_instant(data.get("paid_at") or data.get("paidAt")),
            fee=_kobo_or_none(data.get("fees")),
            raw=data,  # Keep the raw response payload.
        )

    # -- settlement subaccounts ------------------------------------------- #
    def create_subaccount(self, *, business_name, settlement_bank_code, account_number,
                          percentage_charge=0):
        data = self._require_ok(self._post("/subaccount", {
            "business_name": business_name,
            "settlement_bank": settlement_bank_code,
            "account_number": account_number,
            "percentage_charge": percentage_charge,
        }))
        return _subaccount_result(data)

    def update_subaccount(self, *, subaccount_code, business_name, settlement_bank_code,
                          account_number):
        data = self._require_ok(self._put(f"/subaccount/{subaccount_code}", {
            "business_name": business_name,
            "settlement_bank": settlement_bank_code,
            "account_number": account_number,
        }))
        result = _subaccount_result(data)
        return result if result.subaccount_code else SubaccountResult(
            subaccount_code=subaccount_code, account_name=result.account_name, raw=data)

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
            paid_at=_instant(data.get("transferred_at")) if status == "success" else None,
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
            destination_account_number=(
                _dedicated_nuban(data) if direction == "COLLECTION" else ""
            ),
            raw=payload,  # Keep the original normalized payload.
        )


def _kobo_or_none(value):
    """An integer kobo figure from a Paystack field, or None when absent or unreadable."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _subaccount_result(data: dict) -> SubaccountResult:
    """The neutral result for a Paystack subaccount response body."""
    data = data if isinstance(data, dict) else {}
    return SubaccountResult(
        subaccount_code=str(data.get("subaccount_code", "") or ""),
        account_name=str(data.get("account_name", "") or ""),
        raw=data,
    )


# Support the dedicated nuban workflow.
def _dedicated_nuban(data: dict) -> str:
    """Return the dedicated virtual account a ``charge.success`` was paid into, if any.

    Paystack reports a transfer into a dedicated account as an ordinary ``charge.success``
    whose channel is ``dedicated_nuban``. The receiving NUBAN is not a top-level field: it
    rides on ``data.authorization.receiver_bank_account_number``, and older/alternate
    payloads repeat it as ``data.metadata.receiver_account_number``. We read both and
    return ``""`` for every other collection event (card, USSD, hosted checkout), so a
    normal charge is never mistaken for a virtual-account deposit.
    """
    auth = data.get("authorization") or {}  # Authorization block, when the event carries one.
    if not isinstance(auth, dict):  # Defend against a provider sending a non-object here.
        auth = {}
    meta = data.get("metadata") or {}  # Paystack sometimes echoes the receiver in metadata.
    if not isinstance(meta, dict):  # Paystack allows metadata to be a bare string.
        meta = {}
    number = (
        auth.get("receiver_bank_account_number")
        or meta.get("receiver_account_number")
        or ""
    )
    return str(number).strip()  # Normalize to a bare string for the local lookup.


def _instant(value):
    """An aware datetime from a Paystack timestamp, or ``None`` when absent or unreadable.

    Paystack reports ISO-8601 instants in UTC (``2026-08-31T20:40:11.000Z``). A value
    without an offset is read as UTC, which is what Paystack means by it. Unreadable
    input yields ``None`` rather than raising: the date is a refinement of the
    booking, and a receipt must never fail to book because a timestamp was odd.
    """
    if not value:
        return None
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    try:
        parsed = parse_datetime(str(value))
    except ValueError:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


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
