"""Gateway-layer models for vs_payments.

This app sits *in front of* the ledger. Nothing here is itself an accounting entry —
the authoritative money movement is always a ``vs_finance`` journal (a customer receipt
for collections, a vendor payment for payouts). These models track the **external PSP
side**: what we asked a provider to do, what it told us, and the raw webhook events that
confirm settlement. Each confirmed collection/payout points at the finance document it
booked, so the gateway record and the ledger entry are linked but decoupled.

Money is integer **kobo** everywhere (reuses ``vs_finance.MoneyField``). All FKs into the
ledger use string references (``"vs_finance.X"``) so this module imports cleanly; the
dependency direction is vs_payments → vs_finance, never the reverse.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from vs_finance.models import MoneyField, TimeStampedModel

from .constants import (
    CollectionChannel,
    CollectionStatus,
    PaymentAuditAction,
    PaymentProvider,
    PayoutBatchStatus,
    PayoutStatus,
    VirtualAccountStatus,
    WebhookStatus,
)


class VirtualAccount(TimeStampedModel):
    """A dedicated virtual NUBAN issued by a provider for self-reconciling collection.

    Money paid into this account is attributable to one payer/customer without a
    checkout step: the provider notifies us by webhook and we book a receipt. One
    customer can hold at most one active account per provider.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="virtual_accounts",
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    customer = models.ForeignKey(
        "vs_finance.Customer", on_delete=models.PROTECT,
        related_name="virtual_accounts", null=True, blank=True,
    )
    deposit_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="virtual_accounts", null=True, blank=True,
        help_text="Bank/cash GL account credited collections into this NUBAN land in.",
    )
    account_number = models.CharField(max_length=20)
    bank_name = models.CharField(max_length=120, blank=True, default="")
    account_name = models.CharField(max_length=200, blank=True, default="")
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT,
        related_name="virtual_accounts", null=True, blank=True,
    )
    provider_reference = models.CharField(
        max_length=128, blank=True, default="",
        help_text="The provider's id for this account (e.g. dedicated_account id).",
    )
    status = models.CharField(
        max_length=12, choices=VirtualAccountStatus.choices,
        default=VirtualAccountStatus.ACTIVE,
    )
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "account_number"],
                name="uniq_payments_va_provider_account",
            ),
            models.UniqueConstraint(
                fields=["entity", "provider", "customer"],
                condition=models.Q(status="ACTIVE", customer__isnull=False),
                name="uniq_payments_va_active_customer_provider",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "provider"]),
            models.Index(fields=["customer"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.account_number} ({self.bank_name})"


class CollectionIntent(TimeStampedModel):
    """A request to collect money from a payer via a provider (money *in*).

    ``reference`` is **our** idempotency key (sent to the provider as the merchant
    reference); ``provider_reference`` is what the provider returns. When settlement is
    confirmed (by webhook or verify), the intent transitions to ``SUCCEEDED`` and a
    ``vs_finance.Payment`` receipt is booked and linked via ``payment``.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="collection_intents",
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    channel = models.CharField(
        max_length=20, choices=CollectionChannel.choices, default=CollectionChannel.CHECKOUT,
    )
    reference = models.CharField(
        max_length=64, unique=True,
        help_text="Our merchant reference / idempotency key for this collection.",
    )  # Reference is unique so we can idempotently retry the same collection without double-charging.
    provider_reference = models.CharField(max_length=128, blank=True, default="")  # The provider's transaction id for this collection (e.g. Paystack transaction reference).
    amount = MoneyField(help_text="Amount to collect, in kobo.")
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT,
        related_name="collection_intents", null=True, blank=True,
    )
    customer = models.ForeignKey(
        "vs_finance.Customer", on_delete=models.PROTECT,
        related_name="collection_intents", null=True, blank=True,
    )
    invoice = models.ForeignKey(
        "vs_finance.Invoice", on_delete=models.PROTECT,
        related_name="collection_intents", null=True, blank=True,
        help_text="Optional invoice this collection settles (auto-allocated on confirm).",
    )
    deposit_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="collection_intents", null=True, blank=True,
        help_text="Bank/cash GL account the booked receipt debits.",
    )
    virtual_account = models.ForeignKey(
        VirtualAccount, on_delete=models.PROTECT,
        related_name="collection_intents", null=True, blank=True,
    )  # The virtual account this collection was paid into (if any, for self-reconciling collections).
    status = models.CharField(
        max_length=12, choices=CollectionStatus.choices, default=CollectionStatus.PENDING,
    )
    payer_email = models.EmailField(blank=True, default="")
    payer_name = models.CharField(max_length=200, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    checkout_url = models.URLField(blank=True, default="", max_length=600)  # The provider's hosted checkout URL for this collection (if any, for redirect flows).
    authorization_code = models.CharField(max_length=128, blank=True, default="")  # The provider's authorization code for this collection (e.g. Paystack authorization_code).
    payment = models.ForeignKey(
        "vs_finance.Payment", on_delete=models.PROTECT,
        related_name="collection_intents", null=True, blank=True,
        help_text="The customer receipt booked when this collection settled.",
    )  # The FK to the booked receipt (if any, when the collection is confirmed).
    metadata = models.JSONField(default=dict, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["provider", "provider_reference"]),
            models.Index(fields=["customer"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.reference} · {self.amount} kobo · {self.status}"

    @property
    def is_terminal(self) -> bool:
        """Whether the collection is in a terminal state (no further updates expected)."""
        from .constants import COLLECTION_TERMINAL
        return self.status in COLLECTION_TERMINAL


class PayoutBatch(TimeStampedModel):
    """A bulk disbursement: one envelope grouping many :class:`PayoutInstruction` rows.

    The batch is the unit operators work with for payroll runs, vendor settlement runs,
    etc. — they assemble many beneficiaries, then submit once. Submission loops the
    existing per-instruction provider transfer (there is no proprietary bank-file export);
    the batch tracks the aggregate so a partially-settled run is visible at a glance.
    ``total_amount``/``item_count`` are denormalised sums of the child instructions, kept
    in sync by the services layer.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="payout_batches",
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    reference = models.CharField(
        max_length=64, unique=True,
        help_text="Our reference / idempotency key for this batch.",
    )
    title = models.CharField(max_length=200, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=PayoutBatchStatus.choices, default=PayoutBatchStatus.DRAFT,
    )
    total_amount = MoneyField(default=0, help_text="Sum of child instruction amounts, in kobo.")
    item_count = models.PositiveIntegerField(default=0)
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT,
        related_name="payout_batches", null=True, blank=True,
    )
    source_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="payout_batches", null=True, blank=True,
        help_text="Default bank/cash GL account the booked payouts credit.",
    )
    metadata = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["provider"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.reference} · {self.item_count} items · {self.total_amount} kobo · {self.status}"

    @property
    def is_terminal(self) -> bool:
        from .constants import PAYOUT_BATCH_TERMINAL
        return self.status in PAYOUT_BATCH_TERMINAL


class PayoutInstruction(TimeStampedModel):
    """A request to send money out of the entity via a provider (money *out*).

    Mirrors :class:`CollectionIntent` on the disbursement side. On confirmation a
    ``vs_procurement.VendorPayment`` (or a generic bank disbursement journal) is booked;
    its id is stored in ``vendor_payment_id`` so the gateway record links to the ledger
    without this app hard-FKing the procurement schema.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="payout_instructions",
    )
    batch = models.ForeignKey(
        PayoutBatch, on_delete=models.PROTECT, related_name="instructions",
        null=True, blank=True,
        help_text="The bulk batch this instruction belongs to, if any.",
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    reference = models.CharField(
        max_length=64, unique=True,
        help_text="Our merchant reference / idempotency key for this payout.",
    )
    provider_reference = models.CharField(max_length=128, blank=True, default="")
    amount = MoneyField(help_text="Amount to disburse, in kobo.")
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT,
        related_name="payout_instructions", null=True, blank=True,
    )
    beneficiary_name = models.CharField(max_length=200)
    beneficiary_account_number = models.CharField(max_length=20)
    beneficiary_bank_code = models.CharField(max_length=20, blank=True, default="")
    recipient_code = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Provider-side transfer recipient id (e.g. Paystack recipient_code).",
    )
    source_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="payout_instructions", null=True, blank=True,
        help_text="Bank/cash GL account the booked payout credits.",
    )
    narration = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=PayoutStatus.choices, default=PayoutStatus.PENDING,
    )
    # Loose link to the booked ledger document (no hard FK into vs_procurement).
    vendor_source_type = models.CharField(max_length=64, blank=True, default="")
    vendor_source_id = models.CharField(max_length=64, blank=True, default="")
    vendor_payment_id = models.IntegerField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["provider", "provider_reference"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.reference} · {self.amount} kobo · {self.status}"

    @property
    def is_terminal(self) -> bool:
        from .constants import PAYOUT_TERMINAL
        return self.status in PAYOUT_TERMINAL


class WebhookEvent(TimeStampedModel):
    """A raw inbound provider webhook — stored verbatim, deduplicated, then dispatched.

    The store is the idempotency backbone: ``dedupe_key`` is unique, so a provider
    retrying the same event can never drive a second receipt/payout. We persist the raw
    body and headers for audit/replay regardless of whether processing succeeds.
    """

    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    event_type = models.CharField(max_length=64, blank=True, default="")
    provider_reference = models.CharField(
        max_length=128, blank=True, default="",
        help_text="The transaction/transfer reference this event concerns.",
    )
    dedupe_key = models.CharField(
        max_length=200, unique=True,
        help_text="Stable idempotency key (provider event id, else a body hash).",
    )
    signature = models.CharField(max_length=256, blank=True, default="")
    verified = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=WebhookStatus.choices, default=WebhookStatus.RECEIVED,
    )
    headers = models.JSONField(default=dict, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    raw_body = models.TextField(blank=True, default="")
    error = models.CharField(max_length=255, blank=True, default="")
    collection = models.ForeignKey(
        CollectionIntent, on_delete=models.SET_NULL,
        related_name="webhook_events", null=True, blank=True,
    )
    payout = models.ForeignKey(
        PayoutInstruction, on_delete=models.SET_NULL,
        related_name="webhook_events", null=True, blank=True,
    )
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["provider", "event_type"]),
            models.Index(fields=["provider_reference"]),
            models.Index(fields=["status"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_type}:{self.dedupe_key}"


class PaymentEvent(TimeStampedModel):
    """Append-only, immutable gateway action log (the payments-side audit trail).

    Complements the ledger's own immutable journals and ``vs_finance.FinanceAuditLog``:
    those capture the *accounting*; this captures the *gateway* actions around it
    (initiation, confirmation, failure, webhook receipt) including rejected attempts.
    Rows are never updated or deleted.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT,
        related_name="payment_events", null=True, blank=True,
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices, blank=True, default="")
    action = models.CharField(max_length=32, choices=PaymentAuditAction.choices)
    reference = models.CharField(max_length=64, blank=True, default="")
    succeeded = models.BooleanField(default=True)
    message = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["entity", "action"]),
            models.Index(fields=["reference"]),
        ]
        ordering = ["-id"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("PaymentEvent rows are immutable and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("PaymentEvent rows are immutable and cannot be deleted.")

    def __str__(self) -> str:
        return f"{self.action} · {self.reference} · {'ok' if self.succeeded else 'fail'}"
