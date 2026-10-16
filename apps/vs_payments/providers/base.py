"""Provider-neutral payment interface.  # Shared abstractions for all payment providers.

Two capabilities, kept separate per the build plan: :class:`CollectionProvider` (pull
money in - hosted checkout, dedicated virtual accounts, verification) and
:class:`PayoutProvider` (push money out - bank transfers). A concrete provider (Paystack,
Fake) implements both plus the webhook contract in :class:`WebhookCapable`.

Every method speaks in **neutral result dataclasses** carrying integer **kobo** and our
own status vocabulary (``CollectionStatus`` / ``PayoutStatus`` string values), so the
services and the ledger never learn a provider's wire format. The raw provider payload is
always preserved on ``.raw`` for audit.  # Keep provider-specific details out of the core flow.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime


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
    paid_at: datetime | None = None  # Aware instant the payer paid, when the provider says.
    fee: int | None = None  # Kobo the provider kept from the payment; None when unreported.
    paid_via: str = ""  # PaidVia value: how the payer paid; "" when unreported.
    raw: dict = field(default_factory=dict)  # Full provider payload preserved verbatim.

    @property
    # Handle the paid workflow.
    def paid(self) -> bool:
        return self.status == "SUCCEEDED"  # Only succeeded collections count as paid.


@dataclass
class SubaccountResult:
    """Outcome of creating or refreshing a settlement subaccount.

    A subaccount is the provider's route for settling a payment straight into one
    bank account instead of the merchant's own balance.
    """

    subaccount_code: str  # The provider's handle, named on each checkout it settles.
    account_name: str = ""  # The account holder's name as the provider resolved it.
    raw: dict = field(default_factory=dict)  # Raw provider response for traceability.


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
    paid_at: datetime | None = None  # Aware instant the money left, when the provider says.
    raw: dict = field(default_factory=dict)  # Raw PSP response payload.

    @property
    # Handle the paid workflow.
    def paid(self) -> bool:
        return self.status == "PAID"  # PAID is the success state for outbound transfers.


@dataclass
class RefundResult:
    """Outcome of asking a provider to return a payment, or of checking on that request.

    ``status`` is a :class:`~vs_payments.constants.ProviderRefundStatus` value:
    ``PROCESSING`` while the provider has not finished, ``PROCESSED`` once it has
    returned the money, ``FAILED`` when it will not. ``provider_status`` keeps the
    provider's own word (Paystack's ``pending``, ``processing``, ``needs-attention``,
    ``processed``, ``failed``). ``amount`` is kobo, ``0`` when the provider reports none.
    """

    transaction_reference: str  # The payment's reference as the provider knows it.
    provider_reference: str = ""  # The provider's id for the refund.
    status: str = "PROCESSING"
    provider_status: str = ""
    amount: int = 0
    currency: str = "NGN"
    failure_reason: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class TerminalResult:
    """Outcome of opening or fetching a provider payment terminal.

    ``code`` is the provider's handle for it. ``details`` is the provider's own
    description of the terminal, kept as it answered it: the payment methods it
    accepts (a payment page, a dedicated transfer account) and anything else it
    reports, for the screens that show a branch how to take payment.
    """

    code: str
    name: str = ""
    active: bool = True
    details: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class SplitResult:
    """Outcome of creating a settlement split that routes a payment to a subaccount."""

    split_code: str
    raw: dict = field(default_factory=dict)


@dataclass
class AccountResolution:
    """Who the provider says holds a bank account (account-name enquiry)."""

    account_number: str  # The account asked about.
    account_name: str  # The holder's name as the bank registered it.
    raw: dict = field(default_factory=dict)  # Raw provider response for traceability.


@dataclass
class SettlementRecord:
    """One automatic settlement: the provider moving the merchant's money to its own bank.

    ``status`` is ``"SETTLED"`` once the money has left for the bank,
    ``"PENDING"`` while the provider is still paying it, ``"FAILED"`` when the
    bank refused it, and ``"UNKNOWN"`` for a state the adapter does not
    recognise. ``amount`` is the kobo that reached the bank, ``None`` when the
    provider's record carries no readable figure. ``subaccount`` names the
    subaccount the settlement paid, empty for the merchant's own balance.
    """

    settlement_id: str
    status: str
    currency: str = ""
    amount: int | None = None
    settled_at: datetime | None = None
    subaccount: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
# Group behavior for Webhook Parse Result.
class WebhookParseResult:
    """Normalised view of an inbound webhook event.

    ``direction`` is a :class:`~vs_payments.constants.PaymentDirection` value:
    ``"COLLECTION"`` or ``"PAYOUT"``, whose ``status`` is the matching neutral status
    value, or ``"DISPUTE"`` (a chargeback) or ``"REFUND"`` (a refund made at the
    provider), which name the earlier payment they concern by ``reference`` or
    ``provider_reference``. A dispute event that resolves the dispute carries a
    :class:`~vs_payments.constants.DisputeOutcome` as its ``status``; any other
    carries the provider's own status. ``dedupe_key`` is the stable idempotency key extracted
    from the event.
    """

    event_type: str  # Provider event type name.
    direction: str  # A PaymentDirection value.
    reference: str = ""              # our merchant reference if echoed back  # Merchant reference when available.
    provider_reference: str = ""  # PSP-side reference used for matching.
    status: str = ""                 # CollectionStatus / PayoutStatus value  # Neutral lifecycle state.
    amount: int = 0  # Settled amount in kobo.
    currency: str = "NGN"  # Settlement currency code.
    dedupe_key: str = ""  # Stable idempotency key extracted from the event.
    # The NUBAN the payer transferred *into*, for money-in events that name one.
    #
    # A dedicated virtual account collects unsolicited transfers: there is no checkout
    # and therefore no local record to match on ``reference``. The destination account
    # is the only thing in the event that ties the money to a payer we know, so the
    # neutral shape has to carry it or the webhook layer can never resolve the deposit.
    # Empty for hosted-checkout charges, card captures and every payout event.
    destination_account_number: str = ""
    # For a payment made on a branch's provider payment terminal: the terminal's code,
    # the payer's email, and who the money came from (a transfer's sender or a card
    # holder) as the event reports them. The provider never says which customer paid.
    terminal_code: str = ""
    payer_email: str = ""
    sender_name: str = ""
    sender_bank: str = ""
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
                        metadata: dict | None = None, subaccount: str = "") -> CheckoutResult:
        """Create a hosted checkout session.

        ``subaccount``, when given, settles the payment to that subaccount's bank
        account, and the subaccount bears the provider's fee. Callers pass it only
        when set, so an adapter written before subaccounts still serves the rest.
        """

    @abc.abstractmethod
    # Handle the create virtual account workflow.
    def create_virtual_account(self, *, reference: str, customer_name: str,
                               customer_email: str = "", preferred_bank: str = "",
                               metadata: dict | None = None,
                               subaccount: str = "") -> VirtualAccountResult:
        """Provision a dedicated collection account, settling to ``subaccount`` when given.

        ``preferred_bank`` is the provider's own identifier for the issuing bank
        (a slug such as ``wema-bank`` at Paystack), not the bank code used for
        transfers. Empty leaves the choice to the provider's adapter.
        """

    def create_subaccount(self, *, business_name: str, settlement_bank_code: str,
                          account_number: str, percentage_charge: float = 0) -> SubaccountResult:
        """Create a subaccount that settles into ``account_number`` at ``settlement_bank_code``.

        ``percentage_charge`` is the share of each payment kept by the main account;
        zero keeps nothing back. A provider without subaccounts refuses.
        """
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} does not offer settlement subaccounts.",
                            provider=self.name)

    def update_subaccount(self, *, subaccount_code: str, business_name: str,
                          settlement_bank_code: str, account_number: str) -> SubaccountResult:
        """Point an existing subaccount at the given bank account. Refused where unsupported."""
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} does not offer settlement subaccounts.",
                            provider=self.name)

    @abc.abstractmethod
    # Handle the verify collection workflow.
    def verify_collection(self, *, reference: str,
                          provider_reference: str = "") -> CollectionStatusResult:
        ...  # Re-check collection status with the PSP.

    def create_refund(self, *, transaction_reference: str, amount: int, currency: str = "NGN",
                      customer_note: str = "", merchant_note: str = "") -> RefundResult:
        """Ask the provider to return ``amount`` kobo of a confirmed payment to its payer.

        ``transaction_reference`` names the payment as the provider knows it. A
        provider that cannot return payments refuses.
        """
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} does not offer refunds.",
                            provider=self.name)

    def verify_refund(self, *, provider_reference: str = "", transaction_reference: str = "",
                      amount: int = 0) -> RefundResult:
        """The provider's current answer for a refund, by its id or, failing that, its payment.

        With no ``provider_reference`` the payment's refunds are listed and the
        one of ``amount`` kobo answers, so a refund whose creation reply was lost
        can still be found.
        """
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} does not offer refunds.",
                            provider=self.name)

    # -- payment terminals ------------------------------------------------- #
    def create_terminal(self, *, name: str, destinations: list, currency: str = "NGN",
                        metadata: dict | None = None) -> TerminalResult:
        """Open a payment terminal: a payment page and transfer details a branch displays.

        ``destinations`` are the people the provider notifies of each payment,
        each ``{"target": <WhatsApp number>, "name": <person>}``. A provider
        without terminals refuses.
        """
        self._no_terminals()

    def fetch_terminal(self, *, code: str) -> TerminalResult:
        """The provider's current description of terminal ``code``."""
        self._no_terminals()

    def deactivate_terminal(self, *, code: str) -> None:
        """Switch terminal ``code`` off. The provider offers no way to switch it back on."""
        self._no_terminals()

    def assign_terminal_destinations(self, *, code: str, destinations: list) -> None:
        """Add ``destinations`` (each ``{"target": number, "name": person}``) to terminal ``code``."""
        self._no_terminals()

    def unassign_terminal_destinations(self, *, code: str, targets: list) -> None:
        """Remove the numbers ``targets`` from terminal ``code``'s notifications."""
        self._no_terminals()

    def create_split(self, *, name: str, subaccount_code: str,
                     currency: str = "NGN") -> SplitResult:
        """Create a split sending all of a payment to ``subaccount_code``, which bears the fee."""
        self._no_terminals()

    def attach_terminal_split(self, *, code: str, split_code: str) -> None:
        """Route terminal ``code``'s payments through ``split_code``."""
        self._no_terminals()

    def detach_terminal_split(self, *, code: str, split_code: str) -> None:
        """Stop routing terminal ``code``'s payments through ``split_code``."""
        self._no_terminals()

    def _no_terminals(self):
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} does not offer payment terminals.",
                            provider=self.name)


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

    def available_balance(self, currency: str = "NGN") -> int:
        """Kobo the provider holds for the merchant in ``currency``, available to transfer.

        A provider that keeps no balance (or cannot report one) raises
        :class:`NotImplementedError`; the held-ledger reconciliation records that
        as a check it could not make.
        """
        raise NotImplementedError(f"{self.name or 'This provider'} reports no balance.")

    def list_settlements(self, *, start, end) -> list:
        """The provider's automatic settlements of the merchant's own balance, ``start`` to ``end``.

        Both are dates, inclusive, and every page is read. Answers
        :class:`SettlementRecord` rows. A provider that never settles the
        merchant's balance on its own raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(f"{self.name or 'This provider'} reports no settlements.")

    def resolve_account(self, *, account_number: str, bank_code: str) -> AccountResolution:
        """Ask the provider who holds ``account_number`` at ``bank_code``.

        Raises :class:`~vs_payments.exceptions.ProviderError` when the provider
        cannot resolve the account (it does not exist, or the enquiry failed).
        A provider without an account-name enquiry refuses.
        """
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} cannot resolve account names.",
                            provider=self.name)

    def list_banks(self) -> list[dict]:
        """The banks the provider can pay into, as ``[{"name", "code"}]`` ordered by name."""
        from ..exceptions import ProviderError

        raise ProviderError(f"{self.name or 'This provider'} lists no banks.", provider=self.name)

    def transfer_fee(self, amount: int) -> int:
        """Kobo the provider charges, on top of ``amount``, to transfer it to a bank account.

        Charged to the sender's balance, not deducted from what arrives. None by
        default; a provider with a fee schedule says so.
        """
        return 0


# Group behavior for Provider.
class Provider(CollectionProvider, PayoutProvider):
    """A provider that can do both directions (Paystack, Fake all do)."""

    def healthcheck(self) -> bool:
        """Verify configured credentials using a read-only provider operation."""
        raise NotImplementedError("This payment provider does not expose a safe connection test.")
