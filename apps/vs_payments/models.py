"""Gateway-layer models for vs_payments.

This app sits *in front of* the ledger. Nothing here is itself an accounting entry -
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
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from vs_finance.models import MoneyField, TimeStampedModel
from vs_finance.money import format_naira

from .constants import (
    CLEARING_STALE_DAYS_RANGE,
    DEFAULT_CLEARING_STALE_DAYS,
    DEFAULT_SETTLEMENT_INTERVAL_DAYS,
    SETTLEMENT_INTERVAL_RANGE,
    CollectionChannel,
    CollectionStatus,
    CustodyMode,
    HeldMovementKind,
    HeldSettlementStatus,
    PaymentAuditAction,
    PaymentProvider,
    PayoutBatchStatus,
    PayoutPurpose,
    PayoutStatus,
    VirtualAccountStatus,
    WebhookStatus,
)


class VirtualAccount(TimeStampedModel):
    """A dedicated virtual NUBAN issued by a provider for self-reconciling collection.

    Money paid into this account is attributable to one payer/customer without a
    checkout step: the provider notifies us by webhook and we book a receipt. One
    customer can hold at most one active account per provider.

    ``branch`` is set when the account is provisioned
    (:func:`vs_payments.services.virtual_account_branch_id`) and is the branch
    whose subaccount the provider settles deposits to when the tenant takes
    payments directly. ``settlement_subaccount`` is that subaccount, named when
    the account was created; an account without one settles to the platform's
    provider balance, so its deposits are held for the branch
    (:mod:`vs_payments.held`).
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="virtual_accounts",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="payment_virtual_accounts", null=True, blank=True,
        help_text="The branch whose money arrives here: its customer's, else its deposit bank's.",
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
    settlement_subaccount = models.CharField(
        max_length=64, blank=True, default="",
        help_text="The provider subaccount deposits settle to; blank when they settle to "
                  "the platform's balance.",
    )
    replaced_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="replaces", null=True, blank=True,
        help_text="The account issued in place of this retired one.",
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

    The receipt debits gateway clearing, not a bank (``clearing_account``): the
    provider has the money, the bank does not yet. ``settlement_entry`` names
    the journal that later moved it to a bank, and the collection is in clearing
    while that is empty or reversed (:attr:`awaits_settlement`). A collection
    booked before clearing existed debited its bank directly and has no
    ``clearing_account``, so it never awaits settlement.

    That journal belongs to the bank statement line it was booked from, which is
    the only thing that may reverse it (unmatching the line). The field is
    deliberately not named ``*journal*``: finance reads a journal's owning document
    from the foreign keys so named (:func:`vs_finance.posting._journal_document_owner`),
    and one here would claim the journal for this collection instead.

    ``held_by_platform`` says the money settled to the platform's provider balance
    rather than a branch subaccount, recorded when the collection is confirmed:
    the platform then owes it to the branch (:mod:`vs_payments.held`) and pays it
    on in a settlement run, which claims it through ``held_settlement``. A
    settlement that failed releases its claim.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="collection_intents",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="payment_collections", null=True, blank=True,
        help_text="The branch the money belongs to (vs_payments.services.collection_branch_id).",
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
    fee = MoneyField(
        null=True, blank=True, default=None,
        help_text="Kobo the provider kept from this payment, as it reported on confirmation.",
    )
    clearing_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="clearing_collections", null=True, blank=True,
        help_text="The gateway clearing account the receipt debited, until settlement.",
    )
    settlement_entry = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT,
        related_name="settled_collections", null=True, blank=True,
        help_text="The journal that moved this payment from clearing to a bank.",
    )
    held_by_platform = models.BooleanField(
        default=False,
        help_text="The money settled to the platform's provider balance, which owes it to the branch.",
    )
    held_settlement = models.ForeignKey(
        "HeldSettlement", on_delete=models.SET_NULL,
        related_name="collections", null=True, blank=True,
        help_text="The settlement run paying this held payment on to its branch's bank.",
    )
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
            models.Index(fields=["entity", "branch", "status"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.reference} · {format_naira(int(self.amount))} · {self.status}"

    @property
    def is_terminal(self) -> bool:
        """Whether the collection is in a terminal state (no further updates expected)."""
        from .constants import COLLECTION_TERMINAL
        return self.status in COLLECTION_TERMINAL

    @property
    def awaits_settlement(self) -> bool:
        """True while this collection's money sits in gateway clearing.

        The row form of :func:`vs_payments.settlement.awaiting_settlement_q`.
        """
        from vs_finance.constants import DocumentStatus

        if self.clearing_account_id is None or self.payment_id is None:
            return False
        if self.status != CollectionStatus.SUCCEEDED or self.payment.status != DocumentStatus.POSTED:
            return False
        if self.held_settlement_id and self.held_settlement.status == HeldSettlementStatus.PAID:
            return False
        return (self.settlement_entry_id is None
                or self.settlement_entry.status != DocumentStatus.POSTED)


class PayoutBatch(TimeStampedModel):
    """A bulk disbursement: one envelope grouping many :class:`PayoutInstruction` rows.

    The batch is the unit operators work with for payroll runs, vendor settlement runs,
    etc. - they assemble many beneficiaries, then submit once. Submission loops the
    existing per-instruction provider transfer (there is no proprietary bank-file export);
    the batch tracks the aggregate so a partially-settled run is visible at a glance.
    ``total_amount``/``item_count`` are denormalised sums of the child instructions, kept
    in sync by the services layer.

    A batch pays from one branch's money: ``branch`` is its lines' branch, and a
    batch whose lines would leave two branches' banks is refused when it is
    assembled (:func:`vs_payments.services.create_payout_batch`). The approval
    engine reads it as the document's branch.

    ``purpose`` says what it pays: suppliers, or (``SETTLEMENT``) the money the
    platform holds for a client branch, paid into that branch's collection
    account. A settlement batch is the platform's, in the platform's books, and
    goes through the platform's own two-person settlement route
    (:func:`vs_payments.held.build_settlement`).
    """

    # vs_workflow document-type token. Every provider submission must carry the exact
    # terminal approved instance for this batch; a missing template fails closed.
    workflow_document_type = "payments.payout_batch"

    # Field the engine reads for a threshold-gated stage's inclusion condition. The
    # denormalised batch total is the right bar: what escalates a batch is the money
    # leaving in one run, not the size of any single beneficiary line.
    workflow_amount_field = "total_amount"

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="payout_batches",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="payout_batches", null=True, blank=True,
        help_text="The one branch every line of the batch pays from.",
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    purpose = models.CharField(
        max_length=12, choices=PayoutPurpose.choices, default=PayoutPurpose.VENDOR,
    )
    reference = models.CharField(
        max_length=64, unique=True,
        help_text="Our stable provider reference for this batch.",
    )
    idempotency_key = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Caller-supplied key that deduplicates batch creation within an entity.",
    )
    request_fingerprint = models.CharField(
        max_length=64, blank=True, default="",
        help_text="SHA-256 of the normalized request bound to the idempotency key.",
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
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_payments_pbatch_entity_idem_key",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["provider"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return (
            f"{self.reference} · {self.item_count} items · "
            f"{format_naira(int(self.total_amount))} · {self.status}"
        )

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
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="payout_instructions", null=True, blank=True,
        help_text="The branch whose bank account the money leaves (its source account's).",
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
        return f"{self.reference} · {format_naira(int(self.amount))} · {self.status}"

    @property
    def is_terminal(self) -> bool:
        from .constants import PAYOUT_TERMINAL
        return self.status in PAYOUT_TERMINAL


class PaymentCustodySettings(TimeStampedModel):
    """How one tenant's online money is held, and when that changes.

    One row per tenant, binding every branch. ``mode`` is the arrangement in
    force from ``effective_from``; a change is never immediate but is stored as
    ``pending_mode`` from ``pending_from``, always the first day of a month, and
    takes over on that day (:func:`vs_payments.custody.custody_mode`). A tenant
    with no row is ``HELD``, which is where every tenant's money has always been.

    ``settlement_interval_days`` is how often a held-mode tenant is paid what the
    platform holds for it. ``clearing_stale_days`` is how long a confirmed payment
    may sit in gateway clearing before the period close warns.

    A move to ``DIRECT`` that has reached its month waits until the platform
    holds nothing for the tenant (:func:`vs_payments.held.apply_custody_switch`);
    ``pending_note`` says what it is waiting for.
    """

    tenant = models.OneToOneField(
        "vs_tenants.Tenant", on_delete=models.CASCADE, related_name="payment_custody",
    )
    mode = models.CharField(max_length=8, choices=CustodyMode.choices, default=CustodyMode.HELD)
    effective_from = models.DateField(null=True, blank=True)
    pending_mode = models.CharField(
        max_length=8, choices=CustodyMode.choices, blank=True, default="",
    )
    pending_from = models.DateField(null=True, blank=True)
    settlement_interval_days = models.PositiveSmallIntegerField(
        default=DEFAULT_SETTLEMENT_INTERVAL_DAYS,
        validators=[
            MinValueValidator(SETTLEMENT_INTERVAL_RANGE[0]),
            MaxValueValidator(SETTLEMENT_INTERVAL_RANGE[1]),
        ],
    )
    clearing_stale_days = models.PositiveSmallIntegerField(
        default=DEFAULT_CLEARING_STALE_DAYS,
        validators=[
            MinValueValidator(CLEARING_STALE_DAYS_RANGE[0]),
            MaxValueValidator(CLEARING_STALE_DAYS_RANGE[1]),
        ],
    )
    pending_note = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Why a pending change whose month has come has not taken effect yet.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        verbose_name_plural = "payment custody settings"

    def __str__(self) -> str:
        return f"{self.tenant_id}: {self.mode}"


class HeldBalance(TimeStampedModel):
    """What the platform's provider balance holds for one branch of a held-mode tenant.

    One row per branch, the running sum of its :class:`HeldMovement` rows: what
    its online payments brought in after the provider's fees, less what its
    settlements, online payouts and chargebacks have taken. It is the row a
    payout or settlement locks while it checks the branch can afford the
    transfer, so two transfers for one branch can never both spend the same
    money. It mirrors the branch's line of the client-funds liability in the
    platform's own books.

    Only a chargeback can take it below zero: the platform's balance lost money
    the branch no longer had with it. A negative balance is what the branch owes
    the platform (``CLIENT_FUNDS_OWED`` in the platform's books); its next
    payments repay it before anything is settled to it again.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="held_balances",
    )
    branch = models.OneToOneField(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="held_balance",
    )
    balance = models.BigIntegerField(default=0, help_text="Kobo held for the branch.")

    class Meta:
        indexes = [models.Index(fields=["tenant"])]
        ordering = ["tenant_id", "branch_id"]

    def __str__(self) -> str:
        return f"{self.branch_id}: {format_naira(int(self.balance))} held"


class HeldMovement(TimeStampedModel):
    """One change in what the platform holds for a branch: its held-funds sub-ledger.

    ``amount`` is signed kobo: a collection raises the balance by what reached the
    provider balance (the payment less the provider's fee); a payout or a
    settlement lowers it by what leaves the provider balance for it, when the
    transfer is dispatched; a release gives that back when the transfer fails; a
    chargeback lowers it by what the payer's bank took. ``balance_after`` is the
    branch's balance once the movement applied.

    ``platform_journal`` is the entry in the platform's own books moving the
    provider balance by ``amount`` against the client-funds liability, or, for
    the part of the movement below zero, against what the branch owes
    (``CLIENT_FUNDS_OWED``), in the platform's own branch.
    ``tenant_journal`` is a chargeback's entry in the tenant's books (Dr
    chargebacks, Cr gateway clearing, in the branch). It belongs to this row, so it cannot be reversed by
    hand. It is empty, with ``journal_error`` saying why, while those books
    cannot take it (no open period, no account), and is posted later
    (:func:`vs_payments.held.post_pending_platform_journals`): the movement itself
    is never refused, because the money it records has already moved.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="held_movements",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="held_movements",
    )
    kind = models.CharField(max_length=12, choices=HeldMovementKind.choices)
    amount = models.BigIntegerField(help_text="Signed kobo: positive raises the held balance.")
    balance_after = models.BigIntegerField(default=0, help_text="The branch's held balance after it.")
    occurred_on = models.DateField()
    collection = models.ForeignKey(
        CollectionIntent, on_delete=models.PROTECT, related_name="held_movements",
        null=True, blank=True,
    )
    payout = models.ForeignKey(
        PayoutInstruction, on_delete=models.PROTECT, related_name="held_movements",
        null=True, blank=True,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    platform_journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT,
        related_name="held_movements", null=True, blank=True,
    )
    journal_error = models.CharField(max_length=255, blank=True, default="")
    tenant_journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT,
        related_name="held_tenant_movements", null=True, blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection"], condition=models.Q(kind="COLLECTION"),
                name="uniq_payments_held_collection_once",
            ),
            models.UniqueConstraint(
                fields=["collection"], condition=models.Q(kind="DISPUTE"),
                name="uniq_payments_held_dispute_once",
            ),
            models.UniqueConstraint(
                fields=["collection"], condition=models.Q(kind="DISPUTE_WON"),
                name="uniq_payments_held_dispute_won_once",
            ),
            models.UniqueConstraint(
                fields=["branch"], condition=models.Q(kind="OPENING"),
                name="uniq_payments_held_opening_once",
            ),
            models.UniqueConstraint(
                fields=["payout", "kind"], condition=models.Q(payout__isnull=False),
                name="uniq_payments_held_payout_kind_once",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch"]),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.kind} {format_naira(int(self.amount))} for branch {self.branch_id}"


class HeldSettlement(TimeStampedModel):
    """One settlement run's payment to one branch of what the platform holds for it.

    Claims every held payment of the branch confirmed before ``cutoff`` and not yet
    settled (through :attr:`CollectionIntent.held_settlement`), so no payment is
    ever paid on twice. What it takes from the branch's held balance is those
    payments less the provider's fees (``fees``), capped at the balance, which
    online payouts may already have drawn on; ``amount`` is that less the
    provider's fee for the transfer itself (``transfer_fee``), which the branch
    bears.

    ``entity`` is the tenant's books, which record the money arriving. The
    transfer is the platform's: ``batch`` is a ``SETTLEMENT`` payout batch in the
    platform's books, paying the branch's collection account, put forward by a
    platform operator and approved by two platform people
    (:mod:`vs_payments.held`). Once the transfer is confirmed the tenant's books
    record ``settlement_journal`` (Dr bank, Dr bank charges, Cr gateway clearing)
    and the payments leave clearing; nobody at the tenant acts. A settlement
    with nothing to transfer (its payments' money was spent on payouts) books
    only its fees.

    A failed transfer marks it ``FAILED`` and releases its payments for the next
    run. At most one is ``PENDING`` per branch, and a run is idempotent per branch
    and day (``run_on``), so a repeated run builds nothing new.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="held_settlements",
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="held_settlements",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="held_settlements",
    )
    status = models.CharField(
        max_length=10, choices=HeldSettlementStatus.choices, default=HeldSettlementStatus.PENDING,
    )
    run_on = models.DateField(help_text="The tenant's day the run built this settlement.")
    final = models.BooleanField(
        default=False, help_text="Built by a switch to direct custody, to pay out everything held.",
    )
    cutoff = models.DateTimeField(help_text="Payments confirmed before this instant are included.")
    gross = MoneyField(default=0, help_text="The claimed payments, in kobo.")
    fees = MoneyField(default=0, help_text="What the provider kept from them, in kobo.")
    amount = MoneyField(default=0, help_text="Kobo transferred to the branch's bank.")
    transfer_fee = MoneyField(
        default=0, help_text="The provider's fee for the transfer, borne by the branch, in kobo.")
    bank_account = models.ForeignKey(
        "vs_finance.BankAccount", on_delete=models.PROTECT,
        related_name="held_settlements",
    )
    batch = models.OneToOneField(
        PayoutBatch, on_delete=models.PROTECT, related_name="held_settlement",
        null=True, blank=True,
    )
    settlement_journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT,
        related_name="held_settlements", null=True, blank=True,
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["branch"], condition=models.Q(status="PENDING"),
                name="uniq_payments_held_settlement_one_pending",
            ),
            models.UniqueConstraint(
                fields=["branch", "run_on", "final"],
                name="uniq_payments_held_settlement_branch_day",
            ),
        ]
        indexes = [models.Index(fields=["entity", "branch", "status"])]
        ordering = ["-id"]

    def __str__(self) -> str:
        return (
            f"Settlement {self.pk} to branch {self.branch_id}: "
            f"{format_naira(int(self.amount))} ({self.status})"
        )


class HeldReconciliation(TimeStampedModel):
    """One day's check that the platform's books agree with its provider balance.

    ``provider_balance`` is what the provider reports holding (``None`` when it
    could not be asked, with ``error`` saying why). ``books_balance`` is what the
    platform's books say it should hold: its provider balance account
    (``provider_account``) plus its own online takings still in transit
    (``own_in_transit``, its own payments less their fees, not yet settled to
    its bank), less what the provider swept to the platform's bank
    (``swept_total``, every :class:`ProviderSweep` counted), plus the platform's
    own takings that left transit through a matched bank line after sweeping
    began (``own_swept_settled``), which ``swept_total`` already took off once.
    ``balance_swept`` is the platform setting in force when the check ran.
    ``held_total`` is the held-funds sub-ledger's sum, which the
    provider balance account mirrors. ``difference`` is the provider's figure
    less the books'. The check agrees when the difference is within
    ``tolerance`` and the account equals the sub-ledger; otherwise it opens a
    system-health incident (``incident_code``) that the next agreeing check
    resolves (:mod:`vs_payments.held_reconciliation`).

    One row per provider, currency and day: a check run again the same day
    replaces that day's figures.
    """

    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    currency = models.CharField(max_length=3, default="NGN")
    checked_on = models.DateField()
    provider_balance = models.BigIntegerField(null=True, blank=True)
    provider_account = models.BigIntegerField(default=0)
    held_total = models.BigIntegerField(default=0)
    own_in_transit = models.BigIntegerField(default=0)
    balance_swept = models.BooleanField(default=False)
    swept_total = models.BigIntegerField(default=0)
    own_swept_settled = models.BigIntegerField(default=0)
    books_balance = models.BigIntegerField(default=0)
    difference = models.BigIntegerField(null=True, blank=True)
    tolerance = models.BigIntegerField(default=0)
    agrees = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True, default="")
    incident_code = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "currency", "checked_on"],
                name="uniq_payments_held_reconciliation_day",
            ),
        ]
        ordering = ["-checked_on", "-id"]

    def __str__(self) -> str:
        if self.agrees:
            shown = "agrees"
        elif self.difference is None:
            shown = "no difference recorded"
        else:
            shown = format_naira(int(self.difference))
        return f"{self.provider} {self.checked_on}: {shown}"


class ProviderSweep(TimeStampedModel):
    """One automatic settlement of the platform's provider balance to its own bank, counted once.

    When the platform setting ``payments.provider_balance_swept`` is on, the
    daily held-ledger check reads the provider's settlement records and keeps
    each successful one here (:mod:`vs_payments.held_reconciliation`). The
    unique key on provider and ``settlement_id`` is what counts a settlement
    exactly once, however often the check runs or retries. ``amount`` is the
    kobo that reached the bank; ``settled_at`` is the provider's instant for it;
    ``recorded_on`` is the platform day of the check that first counted it.
    ``raw`` is the provider's row, kept for support and never served.

    A sweep moves money between two of the platform's own assets (the provider
    balance and its bank) and changes nothing it owes a client branch, so it is
    taken into the check's comparison rather than booked as a journal.
    """

    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    settlement_id = models.CharField(max_length=64)
    currency = models.CharField(max_length=3, default="NGN")
    amount = models.BigIntegerField()
    settled_at = models.DateTimeField(null=True, blank=True)
    provider_status = models.CharField(max_length=32, blank=True, default="")
    recorded_on = models.DateField()
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "settlement_id"],
                name="uniq_payments_provider_sweep",
            ),
        ]
        ordering = ["-settled_at", "-id"]

    def __str__(self) -> str:
        return f"{self.provider} sweep {self.settlement_id}: {format_naira(int(self.amount))}"


class WebhookEvent(TimeStampedModel):
    """A raw inbound provider webhook - stored verbatim, deduplicated, then dispatched.

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
    Rows are never updated or deleted: refused here in Python and at the database by
    triggers, so a queryset write is refused too. The user columns are PROTECT, so a
    person who appears in the log is deactivated, never deleted.

    ``actor_user`` is the person in whose name the action ran. Under a proxy (an
    impersonation session) that is the impersonated person, and ``proxied_by``
    names the real person at the keyboard; it is null otherwise.
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
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="+", null=True, blank=True,
    )
    proxied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
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
