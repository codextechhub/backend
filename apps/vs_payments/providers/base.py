"""Provider-neutral payment interface.

Two capabilities, kept separate per the build plan: :class:`CollectionProvider` (pull
money in — hosted checkout, dedicated virtual accounts, verification) and
:class:`PayoutProvider` (push money out — bank transfers). A concrete provider (OPay,
Paystack, Fake) implements both plus the webhook contract in :class:`WebhookCapable`.

Every method speaks in **neutral result dataclasses** carrying integer **kobo** and our
own status vocabulary (``CollectionStatus`` / ``PayoutStatus`` string values), so the
services and the ledger never learn a provider's wire format. The raw provider payload is
always preserved on ``.raw`` for audit.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Neutral result types                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class CheckoutResult:
    """Outcome of creating a hosted checkout / redirect collection."""

    reference: str
    provider_reference: str = ""
    checkout_url: str = ""
    authorization_code: str = ""
    status: str = "PENDING"          # CollectionStatus value
    raw: dict = field(default_factory=dict)


@dataclass
class VirtualAccountResult:
    """Outcome of provisioning a dedicated virtual NUBAN."""

    account_number: str
    bank_name: str = ""
    account_name: str = ""
    provider_reference: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class CollectionStatusResult:
    """Outcome of verifying a collection (poll or post-webhook confirm)."""

    reference: str
    provider_reference: str = ""
    status: str = "PENDING"          # CollectionStatus value
    amount: int = 0                  # kobo, as reported by the provider
    currency: str = "NGN"
    raw: dict = field(default_factory=dict)

    @property
    def paid(self) -> bool:
        return self.status == "SUCCEEDED"


@dataclass
class TransferResult:
    """Outcome of creating or verifying a payout/transfer."""

    reference: str
    provider_reference: str = ""
    status: str = "PENDING"          # PayoutStatus value
    recipient_code: str = ""
    failure_reason: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def paid(self) -> bool:
        return self.status == "PAID"


@dataclass
class WebhookParseResult:
    """Normalised view of an inbound webhook event.

    ``direction`` is ``"COLLECTION"`` or ``"PAYOUT"``; ``status`` is the matching neutral
    status value. ``dedupe_key`` is the stable idempotency key extracted from the event.
    """

    event_type: str
    direction: str                   # PaymentDirection value
    reference: str = ""              # our merchant reference if echoed back
    provider_reference: str = ""
    status: str = ""                 # CollectionStatus / PayoutStatus value
    amount: int = 0
    currency: str = "NGN"
    dedupe_key: str = ""
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Capability interfaces                                                        #
# --------------------------------------------------------------------------- #

class WebhookCapable(abc.ABC):
    """Signature verification + event normalisation for inbound webhooks."""

    @abc.abstractmethod
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        """Return True iff ``raw_body`` carries a valid signature for this provider."""

    @abc.abstractmethod
    def parse_webhook(self, *, payload: dict, raw_body: bytes, headers: dict) -> WebhookParseResult:
        """Normalise a verified webhook body into a :class:`WebhookParseResult`."""


class CollectionProvider(WebhookCapable):
    """Pull money in."""

    name: str = ""

    @abc.abstractmethod
    def create_checkout(self, *, reference: str, amount: int, currency: str,
                        customer_email: str = "", customer_name: str = "",
                        narration: str = "", callback_url: str = "",
                        metadata: dict | None = None) -> CheckoutResult:
        ...

    @abc.abstractmethod
    def create_virtual_account(self, *, reference: str, customer_name: str,
                               customer_email: str = "", bank_code: str = "",
                               metadata: dict | None = None) -> VirtualAccountResult:
        ...

    @abc.abstractmethod
    def verify_collection(self, *, reference: str,
                          provider_reference: str = "") -> CollectionStatusResult:
        ...


class PayoutProvider(WebhookCapable):
    """Push money out."""

    name: str = ""

    @abc.abstractmethod
    def create_transfer(self, *, reference: str, amount: int, currency: str,
                        account_number: str, bank_code: str, account_name: str = "",
                        narration: str = "", metadata: dict | None = None) -> TransferResult:
        ...

    @abc.abstractmethod
    def verify_transfer(self, *, reference: str,
                        provider_reference: str = "") -> TransferResult:
        ...


class Provider(CollectionProvider, PayoutProvider):
    """A provider that can do both directions (OPay, Paystack, Fake all do)."""
