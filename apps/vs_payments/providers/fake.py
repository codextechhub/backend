"""In-memory fake provider for tests and local development.

Implements the full :class:`~vs_payments.providers.base.Provider` contract without any
network I/O, so the whole collection/payout/webhook flow is exercisable deterministically.
Signatures use HMAC-SHA512 over the raw body with :attr:`secret` (mirrors the real
providers' scheme), and :meth:`build_webhook` produces a correctly-signed event body a
test can feed straight into the webhook ingestion path.

The secret has no default. The registry builds this provider only where
``PAYMENTS_FAKE_PROVIDER_ENABLED`` is on, and passes ``PAYMENTS_FAKE_WEBHOOK_SECRET``;
a test that constructs one names its own secret. An empty secret verifies nothing,
because an HMAC keyed with the empty string is one anybody can compute.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from .base import (
    CheckoutResult,
    CollectionStatusResult,
    Provider,
    SubaccountResult,
    TransferResult,
    VirtualAccountResult,
    WebhookParseResult,
)

SIGNATURE_HEADER = "x-fake-signature"  # Header name used by the fake webhook verifier.


# Group behavior for Fake Provider.
class FakeProvider(Provider):
    """A deterministic, network-free provider implementation."""

    name = "FAKE"  # Registry key for the fake provider.

    def __init__(self, *, secret: str, bank_name: str = "Fake MFB"):
        self.secret = secret  # HMAC secret used to sign fake webhooks.
        self.bank_name = bank_name  # Display bank name used for virtual accounts.
        # Lets a test force the next verify result without a webhook round-trip.  # Override verification outcomes.
        self.forced_status: dict[str, str] = {}
        # Lets a test force the provider-reported settled amount (kobo) per reference.  # Override the verified amount.
        self.forced_amount: dict[str, int] = {}
        # Lets a test say when the provider records the payment as made, per reference.
        self.forced_paid_at: dict = {}
        # The fee (kobo) verify reports per reference; ``default_fee`` for the rest.
        self.forced_fee: dict[str, int] = {}
        self.default_fee: int | None = 0
        # Every subaccount named on a checkout or virtual account, by reference.
        self.subaccounts_named: dict[str, str] = {}
        # Subaccounts created or refreshed, by code: the bank details they settle to.
        self.subaccounts: dict[str, dict] = {}
        # The fee (kobo) every transfer costs the sender's balance.
        self.transfer_fee_kobo: int = 0
        # What ``available_balance`` reports, by currency; a test sets it.
        self.balances: dict[str, int] = {}
        # The automatic settlements ``list_settlements`` reports; a test sets them.
        self.settlements: list = []

    def healthcheck(self) -> bool:
        return True

    # -- collection --------------------------------------------------------- #  # Money-in behavior.
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None,
                        subaccount=""):
        if subaccount:  # Remember the settlement route a test may assert on.
            self.subaccounts_named[reference] = subaccount
        return CheckoutResult(  # Return a deterministic hosted checkout result.
            reference=reference,  # Echo the merchant reference.
            provider_reference=f"FAKE-{reference}",  # Fake provider-side reference.
            checkout_url=f"https://fake.test/checkout/{reference}",  # Predictable checkout URL.
            authorization_code=f"AUTH-{reference}",  # Predictable authorization code.
            status="PENDING",  # Fake checkout starts pending.
            raw={"amount": amount, "currency": currency, "metadata": metadata or {}},  # Preserve core request fields.
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None, subaccount=""):
        if subaccount:  # Remember the settlement route a test may assert on.
            self.subaccounts_named[reference] = subaccount
        # Deterministic 10-digit NUBAN from the reference.  # Keep account generation repeatable.
        digits = str(abs(hash(reference)) % 10_000_000_000).rjust(10, "0")  # Normalize the hash into a 10-digit string.
        return VirtualAccountResult(  # Return a predictable virtual account payload.
            account_number=digits,  # Fake account number.
            bank_name=self.bank_name,  # Configured fake bank name.
            account_name=customer_name,  # Use the customer name as the account name.
            provider_reference=f"FAKE-VA-{reference}",  # Fake provider reference for the virtual account.
            raw={"reference": reference},  # Keep the seed reference for traceability.
        )

    def verify_collection(self, *, reference, provider_reference=""):
        status = self.forced_status.get(reference, "PENDING")
        return CollectionStatusResult(  # Return a deterministic verification result.
            reference=reference,  # Merchant reference being verified.
            provider_reference=provider_reference or f"FAKE-{reference}",  # Provide a predictable provider reference.
            status=status,  # Return the forced or default status.
            amount=self.forced_amount.get(reference, 0),  # Report the forced settled amount (0 = not reported).
            paid_at=self.forced_paid_at.get(reference),  # None unless a test names the paid instant.
            fee=self.forced_fee.get(reference, self.default_fee),
            raw={"forced": status},  # Show where the verification status came from.
        )

    # -- settlement subaccounts ------------------------------------------- #
    def create_subaccount(self, *, business_name, settlement_bank_code, account_number,
                          percentage_charge=0):
        code = f"ACCT_FAKE{len(self.subaccounts) + 1:04d}"
        self.subaccounts[code] = {
            "business_name": business_name, "settlement_bank": settlement_bank_code,
            "account_number": account_number, "percentage_charge": percentage_charge,
        }
        return SubaccountResult(subaccount_code=code, account_name=business_name,
                                raw={"subaccount_code": code, **self.subaccounts[code]})

    def update_subaccount(self, *, subaccount_code, business_name, settlement_bank_code,
                          account_number):
        self.subaccounts[subaccount_code] = {
            **self.subaccounts.get(subaccount_code, {}),
            "business_name": business_name, "settlement_bank": settlement_bank_code,
            "account_number": account_number,
        }
        return SubaccountResult(subaccount_code=subaccount_code, account_name=business_name,
                                raw={"subaccount_code": subaccount_code,
                                     **self.subaccounts[subaccount_code]})

    # -- payout ------------------------------------------------------------- #  # Money-out behavior.
    # Handle the create transfer workflow.
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        return TransferResult(  # Return a deterministic transfer response.
            reference=reference,  # Merchant payout reference.
            provider_reference=f"FAKE-TR-{reference}",  # Fake provider transfer id.
            status="PROCESSING",  # Transfers start in flight.
            recipient_code=f"RCP-{account_number}",  # Predictable recipient code.
            raw={"amount": amount, "account_number": account_number},  # Keep core transfer inputs.
        )

    def transfer_fee(self, amount):
        return self.transfer_fee_kobo

    def available_balance(self, currency="NGN"):
        return int(self.balances.get(currency.upper(), 0))

    def list_settlements(self, *, start, end):
        """Every settlement a test set whose date falls between ``start`` and ``end`` (UTC days)."""
        return [row for row in self.settlements
                if row.settled_at is None or start <= row.settled_at.date() <= end]

    def verify_transfer(self, *, reference, provider_reference=""):
        status = self.forced_status.get(reference, "PROCESSING")
        return TransferResult(  # Return a deterministic transfer verification result.
            reference=reference,  # Merchant payout reference.
            provider_reference=provider_reference or f"FAKE-TR-{reference}",  # Predictable fake transfer id.
            status=status,  # Forced or default transfer status.
            amount=self.forced_amount.get(reference, 0),  # Report the forced settled amount (0 = not reported).
            paid_at=self.forced_paid_at.get(reference),  # None unless a test names the paid instant.
            raw={"forced": status},  # Show the origin of the returned state.
        )

    # -- webhooks ----------------------------------------------------------- #  # Webhook signature and parsing.
    # Support the sign workflow.
    def _sign(self, raw_body: bytes) -> str:
        return hmac.new(self.secret.encode(), raw_body, hashlib.sha512).hexdigest()  # Mirror real-provider HMAC signing.

    # Handle the verify signature workflow.
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        if not self.secret:  # No key means no event can be authentic.
            return False
        sent = ""  # Hold the supplied signature if we find one.
        for key, value in (headers or {}).items():  # Walk headers case-insensitively.
            if key.lower() == SIGNATURE_HEADER:  # Look for the fake signature header.
                sent = value  # Capture the supplied signature.
                break  # Exit the current loop.
        return bool(sent) and hmac.compare_digest(sent, self._sign(raw_body))  # Compare supplied and expected signatures.

    # Handle the parse webhook workflow.
    def parse_webhook(self, *, payload, raw_body, headers):
        data = payload.get("data", payload)
        event = payload.get("event", "")
        direction = (
            "PAYOUT" if event.startswith("transfer")
            else "DISPUTE" if event.startswith("charge.dispute")
            else "REFUND" if event.startswith("refund")
            else "COLLECTION"
        )
        status = data.get("status", "")
        if direction == "DISPUTE" and data.get("resolution"):  # Mirrors Paystack's resolve event.
            status = {"declined": "WON", "merchant-accepted": "LOST",
                      "auto-accepted": "LOST"}.get(data["resolution"], status)
        return WebhookParseResult(  # Return a neutral parse result for the webhook pipeline.
            event_type=payload.get("event", ""),
            direction=direction,  # Route to the collection or payout flow.
            reference=data.get("reference", ""),
            provider_reference=str(data.get("id", "")),
            status=status,  # Fake status value.
            amount=int(data.get("amount", 0)),
            currency=data.get("currency", "NGN"),
            dedupe_key=f"FAKE:{payload.get('event', '')}:{data.get('reference', '')}",
            # Mirrors Paystack: a transfer into a dedicated NUBAN names the receiving
            # account on the event, and nothing else ties it to a payer we know.
            destination_account_number=(
                str(data.get("receiver_account_number", "") or "")
                if direction == "COLLECTION" else ""
            ),
            raw=payload,  # Preserve the original payload.
        )

    # -- test helper -------------------------------------------------------- #  # Utilities used in tests.
    # Handle the build webhook workflow.
    def build_webhook(self, *, event: str, reference: str, status: str,
                      amount: int = 0, currency: str = "NGN", provider_id: str = "1",
                      receiver_account_number: str = "", resolution: str = ""):
        """Return ``(raw_body: bytes, headers: dict)`` for a correctly-signed event.

        Pass ``receiver_account_number`` to build a dedicated virtual-account deposit
        (the unsolicited-transfer event); leave it out for an ordinary charge. Pass
        ``resolution`` (``declined`` for a dispute the merchant won,
        ``merchant-accepted`` for one it lost) with ``charge.dispute.resolve``.
        """
        data = {  # Build the event data block in the shape the parser expects.
            "reference": reference, "status": status, "amount": amount,  # Core event fields.
            "currency": currency, "id": provider_id,  # Provider id used by the parser.
        }
        if resolution:  # Only a dispute resolution carries one.
            data["resolution"] = resolution
        if receiver_account_number:  # Only deposits carry a receiving account.
            data["receiver_account_number"] = receiver_account_number  # Name the destination NUBAN.
        body = json.dumps({"event": event, "data": data}).encode()  # Encode the payload as bytes.
        return body, {SIGNATURE_HEADER: self._sign(body)}  # Return the body and matching fake signature.
