"""Provider-neutral payment interface.  # Shared abstractions for all payment providers.

Two capabilities, kept separate per the build plan: :class:`CollectionProvider` (pull
money in — hosted checkout, dedicated virtual accounts, verification) and
:class:`PayoutProvider` (push money out — bank transfers). A concrete provider (OPay,
Paystack, Fake) implements both plus the webhook contract in :class:`WebhookCapable`.

Every method speaks in **neutral result dataclasses** carrying integer **kobo** and our
own status vocabulary (``CollectionStatus`` / ``PayoutStatus`` string values), so the
services and the ledger never learn a provider's wire format. The raw provider payload is
always preserved on ``.raw`` for audit.  # Keep provider-specific details out of the core flow.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #  # Group provider-neutral result types here.
# Neutral result types                                                        #  # Shared dataclasses for PSP responses.
# --------------------------------------------------------------------------- #  # End result section.

@dataclass
# Group behavior for Checkout Result.
class CheckoutResult:
    """Outcome of creating a hosted checkout / redirect collection."""

    reference: str  # Merchant reference used to correlate this checkout.
    provider_reference: str = ""  # PSP transaction reference.
    checkout_url: str = ""  # Hosted checkout URL returned by the PSP.
    authorization_code: str = ""  # Optional authorization token from the PSP.
    status: str = "PENDING"          # CollectionStatus value  # Neutral collection lifecycle state.
    raw: dict = field(default_factory=dict)  # Unmodified PSP payload for audit/debugging.


@dataclass
# Group behavior for Virtual Account Result.
class VirtualAccountResult:
    """Outcome of provisioning a dedicated virtual NUBAN."""

    account_number: str  # Virtual account number issued by the PSP.
    bank_name: str = ""  # Name of the issuing bank.
    account_name: str = ""  # Name attached to the account at the PSP.
    provider_reference: str = ""  # PSP-side identifier for the virtual account.
    raw: dict = field(default_factory=dict)  # Raw provider response for traceability.


@dataclass
# Define Collection Status Result values.
class CollectionStatusResult:
    """Outcome of verifying a collection (poll or post-webhook confirm)."""

    reference: str  # Merchant reference for the collection.
    provider_reference: str = ""  # Provider-side collection identifier.
    status: str = "PENDING"          # CollectionStatus value  # Neutral status string.
    amount: int = 0                  # kobo, as reported by the provider  # Settled amount in kobo.
    currency: str = "NGN"  # Settlement currency code.
    raw: dict = field(default_factory=dict)  # Full provider payload preserved verbatim.

    @property
    # Handle the paid workflow.
    def paid(self) -> bool:
        return self.status == "SUCCEEDED"  # Only succeeded collections count as paid.


@dataclass
# Group behavior for Transfer Result.
class TransferResult:
    """Outcome of creating or verifying a payout/transfer."""

    reference: str  # Merchant reference for the payout.
    provider_reference: str = ""  # Provider-side transfer identifier.
    status: str = "PENDING"          # PayoutStatus value  # Neutral payout lifecycle state.
    amount: int = 0                  # kobo, as reported by the provider (0 = not reported)  # Settled transfer amount.
    recipient_code: str = ""  # PSP recipient code when the transfer is created.
    failure_reason: str = ""  # Human-readable failure explanation, if any.
    raw: dict = field(default_factory=dict)  # Raw PSP response payload.

    @property
    # Handle the paid workflow.
    def paid(self) -> bool:
        return self.status == "PAID"  # PAID is the success state for outbound transfers.


@dataclass
# Group behavior for Webhook Parse Result.
class WebhookParseResult:
    """Normalised view of an inbound webhook event.

    ``direction`` is ``"COLLECTION"`` or ``"PAYOUT"``; ``status`` is the matching neutral
    status value. ``dedupe_key`` is the stable idempotency key extracted from the event.
    """

    event_type: str  # Provider event type name.
    direction: str                   # PaymentDirection value  # COLLECTION or PAYOUT.
    reference: str = ""              # our merchant reference if echoed back  # Merchant reference when available.
    provider_reference: str = ""  # PSP-side reference used for matching.
    status: str = ""                 # CollectionStatus / PayoutStatus value  # Neutral lifecycle state.
    amount: int = 0  # Settled amount in kobo.
    currency: str = "NGN"  # Settlement currency code.
    dedupe_key: str = ""  # Stable idempotency key extracted from the event.
    raw: dict = field(default_factory=dict)  # Normalized raw payload for replay/audit.


# --------------------------------------------------------------------------- #  # Provider capabilities are defined below.
# Capability interfaces                                                        #  # Abstract contracts for concrete adapters.
# --------------------------------------------------------------------------- #  # End interface section.

# Group behavior for Webhook Capable.
class WebhookCapable(abc.ABC):
    """Signature verification + event normalisation for inbound webhooks."""

    @abc.abstractmethod
    # Handle the verify signature workflow.
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        """Return True iff ``raw_body`` carries a valid signature for this provider."""  # Reject forged events.

    @abc.abstractmethod
    # Handle the parse webhook workflow.
    def parse_webhook(self, *, payload: dict, raw_body: bytes, headers: dict) -> WebhookParseResult:
        """Normalise a verified webhook body into a :class:`WebhookParseResult`."""  # Map provider payloads to our neutral shape.


# Group behavior for Collection Provider.
class CollectionProvider(WebhookCapable):
    """Pull money in."""

    name: str = ""  # Human-readable provider name.

    @abc.abstractmethod
    # Handle the create checkout workflow.
    def create_checkout(self, *, reference: str, amount: int, currency: str,
                        customer_email: str = "", customer_name: str = "",
                        narration: str = "", callback_url: str = "",
                        metadata: dict | None = None) -> CheckoutResult:
        ...  # Create a hosted checkout session.

    @abc.abstractmethod
    # Handle the create virtual account workflow.
    def create_virtual_account(self, *, reference: str, customer_name: str,
                               customer_email: str = "", bank_code: str = "",
                               metadata: dict | None = None) -> VirtualAccountResult:
        ...  # Provision a dedicated collection account.

    @abc.abstractmethod
    # Handle the verify collection workflow.
    def verify_collection(self, *, reference: str,
                          provider_reference: str = "") -> CollectionStatusResult:
        ...  # Re-check collection status with the PSP.


# Group behavior for Payout Provider.
class PayoutProvider(WebhookCapable):
    """Push money out."""

    name: str = ""  # Human-readable provider name.

    @abc.abstractmethod
    # Handle the create transfer workflow.
    def create_transfer(self, *, reference: str, amount: int, currency: str,
                        account_number: str, bank_code: str, account_name: str = "",
                        narration: str = "", metadata: dict | None = None) -> TransferResult:
        ...  # Initiate a bank transfer.

    @abc.abstractmethod
    # Handle the verify transfer workflow.
    def verify_transfer(self, *, reference: str,
                        provider_reference: str = "") -> TransferResult:
        ...  # Re-check transfer status with the PSP.


# Group behavior for Provider.
class Provider(CollectionProvider, PayoutProvider):
    """A provider that can do both directions (OPay, Paystack, Fake all do)."""
