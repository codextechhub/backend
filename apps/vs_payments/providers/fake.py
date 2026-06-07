"""In-memory fake provider for tests and local development.

Implements the full :class:`~vs_payments.providers.base.Provider` contract without any
network I/O, so the whole collection/payout/webhook flow is exercisable deterministically.
Signatures use HMAC-SHA512 over the raw body with :attr:`secret` (mirrors the real
providers' scheme), and :meth:`build_webhook` produces a correctly-signed event body a
test can feed straight into the webhook ingestion path.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from .base import (
    CheckoutResult,
    CollectionStatusResult,
    Provider,
    TransferResult,
    VirtualAccountResult,
    WebhookParseResult,
)

SIGNATURE_HEADER = "x-fake-signature"


class FakeProvider(Provider):
    """A deterministic, network-free provider implementation."""

    name = "FAKE"

    def __init__(self, *, secret: str = "fake-secret", bank_name: str = "Fake MFB"):
        self.secret = secret
        self.bank_name = bank_name
        # Lets a test force the next verify result without a webhook round-trip.
        self.forced_status: dict[str, str] = {}

    # -- collection --------------------------------------------------------- #
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None):
        return CheckoutResult(
            reference=reference,
            provider_reference=f"FAKE-{reference}",
            checkout_url=f"https://fake.test/checkout/{reference}",
            authorization_code=f"AUTH-{reference}",
            status="PENDING",
            raw={"amount": amount, "currency": currency, "metadata": metadata or {}},
        )

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               bank_code="", metadata=None):
        # Deterministic 10-digit NUBAN from the reference.
        digits = str(abs(hash(reference)) % 10_000_000_000).rjust(10, "0")
        return VirtualAccountResult(
            account_number=digits,
            bank_name=self.bank_name,
            account_name=customer_name,
            provider_reference=f"FAKE-VA-{reference}",
            raw={"reference": reference},
        )

    def verify_collection(self, *, reference, provider_reference=""):
        status = self.forced_status.get(reference, "PENDING")
        return CollectionStatusResult(
            reference=reference,
            provider_reference=provider_reference or f"FAKE-{reference}",
            status=status,
            raw={"forced": status},
        )

    # -- payout ------------------------------------------------------------- #
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        return TransferResult(
            reference=reference,
            provider_reference=f"FAKE-TR-{reference}",
            status="PROCESSING",
            recipient_code=f"RCP-{account_number}",
            raw={"amount": amount, "account_number": account_number},
        )

    def verify_transfer(self, *, reference, provider_reference=""):
        status = self.forced_status.get(reference, "PROCESSING")
        return TransferResult(
            reference=reference,
            provider_reference=provider_reference or f"FAKE-TR-{reference}",
            status=status,
            raw={"forced": status},
        )

    # -- webhooks ----------------------------------------------------------- #
    def _sign(self, raw_body: bytes) -> str:
        return hmac.new(self.secret.encode(), raw_body, hashlib.sha512).hexdigest()

    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        sent = ""
        for key, value in (headers or {}).items():
            if key.lower() == SIGNATURE_HEADER:
                sent = value
                break
        return bool(sent) and hmac.compare_digest(sent, self._sign(raw_body))

    def parse_webhook(self, *, payload, raw_body, headers):
        data = payload.get("data", payload)
        direction = "PAYOUT" if payload.get("event", "").startswith("transfer") else "COLLECTION"
        status = data.get("status", "")
        return WebhookParseResult(
            event_type=payload.get("event", ""),
            direction=direction,
            reference=data.get("reference", ""),
            provider_reference=str(data.get("id", "")),
            status=status,
            amount=int(data.get("amount", 0)),
            currency=data.get("currency", "NGN"),
            dedupe_key=f"FAKE:{payload.get('event', '')}:{data.get('reference', '')}",
            raw=payload,
        )

    # -- test helper -------------------------------------------------------- #
    def build_webhook(self, *, event: str, reference: str, status: str,
                      amount: int = 0, currency: str = "NGN", provider_id: str = "1"):
        """Return ``(raw_body: bytes, headers: dict)`` for a correctly-signed event."""
        body = json.dumps({
            "event": event,
            "data": {
                "reference": reference, "status": status, "amount": amount,
                "currency": currency, "id": provider_id,
            },
        }).encode()
        return body, {SIGNATURE_HEADER: self._sign(body)}
