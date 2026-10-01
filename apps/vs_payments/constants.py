"""Enumerations and well-known constants for vs_payments.  # Shared payment state and provider identifiers.

The payments app is the *gateway* layer that sits in front of the ledger: it talks to
external PSPs (Paystack) to **collect** money from customers and **pay out** money
to vendors/beneficiaries, then hands the confirmed cash movement to the existing finance
services (``vs_finance.receivables.post_payment`` for receipts;
``vs_procurement.payables.post_vendor_payment`` for payouts). Money is always integer
**kobo** here too - never float.  # Keep gateway values aligned with the ledger.
"""
from __future__ import annotations

from django.db import models


# Group behavior for Payment Provider.
class PaymentProvider(models.TextChoices):
    """The external payment service providers we integrate with."""

    PAYSTACK = "PAYSTACK", "Paystack"  # Paystack live provider.
    FAKE = "FAKE", "Fake (testing)"  # In-memory test provider.


# Group behavior for Payment Direction.
class PaymentDirection(models.TextChoices):
    """Which way money flows relative to the ledger entity."""

    COLLECTION = "COLLECTION", "Collection (money in)"  # Incoming money.
    PAYOUT = "PAYOUT", "Payout (money out)"  # Outgoing money.
    DISPUTE = "DISPUTE", "Dispute (a payer's chargeback)"  # Money a payer's bank claims back.
    REFUND = "REFUND", "Refund (money returned through the provider)"  # A provider-side refund.


# Group behavior for Collection Channel.
class CollectionChannel(models.TextChoices):
    """How a collection is presented to the payer."""

    CHECKOUT = "CHECKOUT", "Hosted checkout / redirect"  # Redirect-style checkout.
    VIRTUAL_ACCOUNT = "VIRTUAL_ACCOUNT", "Dedicated virtual account (NUBAN)"  # Unique account for bank transfer.
    CARD = "CARD", "Card"  # Card-present or card-not-present capture.
    BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"  # Manual or virtual-account bank transfer.
    USSD = "USSD", "USSD"  # USSD-based payment flow.


# Define Collection Status values.
class CollectionStatus(models.TextChoices):
    """Lifecycle of a collection intent.

    ``PENDING`` → created locally, payer not yet acted; ``PROCESSING`` → provider
    acknowledged/awaiting settlement; ``SUCCEEDED`` → confirmed paid (terminal, books a
    receipt); ``FAILED``/``ABANDONED`` → no money so far; ``REFUNDED`` → reversed.

    ``FAILED`` and ``ABANDONED`` are what the provider said at the moment somebody
    asked, not a promise about the checkout. A parent can close the tab (abandoned),
    come back and finish paying on the same reference, so a later provider-confirmed
    success still books. See :data:`COLLECTION_SETTLED`.
    """

    PENDING = "PENDING", "Pending"  # Created locally, not yet settled.
    PROCESSING = "PROCESSING", "Processing"  # Provider acknowledged but not final.
    SUCCEEDED = "SUCCEEDED", "Succeeded"  # Final success, books a receipt.
    FAILED = "FAILED", "Failed"  # Final failure, no cash movement.
    ABANDONED = "ABANDONED", "Abandoned"  # Payer never completed the flow.
    REFUNDED = "REFUNDED", "Refunded"  # Previously succeeded then reversed.


#: Collection states past which no further automatic transition happens.  # Terminal collection states.
COLLECTION_TERMINAL = frozenset(
    {CollectionStatus.SUCCEEDED, CollectionStatus.FAILED,
     CollectionStatus.ABANDONED, CollectionStatus.REFUNDED}
)

#: Collection states that never book again: money was booked, or booked and reversed.
#: The only guard against a second receipt, so nothing outside it may book.
COLLECTION_SETTLED = frozenset({CollectionStatus.SUCCEEDED, CollectionStatus.REFUNDED})

#: No-money outcomes a provider can still overturn with a confirmed success on the
#: same reference. The recovery sweep re-checks them only briefly, but neither ever
#: stops a success from booking.
COLLECTION_PROVISIONAL_FAILURES = frozenset(
    {CollectionStatus.FAILED, CollectionStatus.ABANDONED}
)


# Define Payout Status values.
class PayoutStatus(models.TextChoices):
    """Lifecycle of a payout instruction (money leaving the entity)."""

    PENDING = "PENDING", "Pending"  # Created locally, not yet sent.
    PROCESSING = "PROCESSING", "Processing"  # Sent to provider, awaiting final settlement.
    PAID = "PAID", "Paid"  # Final success, books the vendor payment.
    FAILED = "FAILED", "Failed"  # Final failure, no settlement.
    REVERSED = "REVERSED", "Reversed"  # Settled then reversed.


#: Payout states past which no further automatic transition happens.  # Terminal payout states.
PAYOUT_TERMINAL = frozenset(
    {PayoutStatus.PAID, PayoutStatus.FAILED, PayoutStatus.REVERSED}
)


# Define Payout Batch Status values.
class PayoutBatchStatus(models.TextChoices):
    """Lifecycle of a bulk-disbursement batch grouping many payout instructions.

    ``DRAFT`` → created locally with child instructions but not yet submitted;
    ``PROCESSING`` → submitted, at least one child accepted by the provider and none
    finished failing; ``COMPLETED`` → every child reached ``PAID``;
    ``PARTIALLY_COMPLETED`` → batch finished but a mix of paid/failed children;
    ``FAILED`` → every child failed to submit/settle.
    """

    DRAFT = "DRAFT", "Draft"  # Created locally, not yet submitted.
    PROCESSING = "PROCESSING", "Processing"  # At least one child is in flight.
    COMPLETED = "COMPLETED", "Completed"  # Every child reached PAID.
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED", "Partially completed"  # Mixed success/failure.
    FAILED = "FAILED", "Failed"  # Every child failed.


#: Batch states past which no further automatic transition happens.  # Terminal batch states.
PAYOUT_BATCH_TERMINAL = frozenset(
    {PayoutBatchStatus.COMPLETED, PayoutBatchStatus.PARTIALLY_COMPLETED,
     PayoutBatchStatus.FAILED}
)

#: Batch states in which a batch may still owe an unsent instruction, and against
#: which money may therefore still move. The complement of the terminal set, and the
#: two together cover the enum: a status in neither is one nobody has decided about,
#: so add every new status to exactly one of them.
#:
#: A batch turns PROCESSING as soon as a single child is accepted or settled, so a
#: batch that was sent halfway through and still holds PENDING instructions sits here
#: rather than in DRAFT. Anything looking for undispatched work must accept both.
PAYOUT_BATCH_DISPATCHABLE = frozenset(
    {PayoutBatchStatus.DRAFT, PayoutBatchStatus.PROCESSING}
)


# Define Virtual Account Status values.
class VirtualAccountStatus(models.TextChoices):
    """Whether a virtual account is offered, and what a deposit into it does.

    ``INACTIVE`` is switched off: a deposit into it is held for review. ``RETIRED``
    was replaced by a new number when the tenant's money started settling
    directly: it is no longer offered, but a deposit into it still books to its
    customer, and the platform passes the money on in the next settlement run.
    """

    ACTIVE = "ACTIVE", "Active"  # Available for incoming transfers.
    INACTIVE = "INACTIVE", "Inactive"  # No longer offered for new transfers.
    RETIRED = "RETIRED", "Retired"  # Replaced; deposits still book and are passed on.


# Define Webhook Status values.
class WebhookStatus(models.TextChoices):
    """Processing state of a raw inbound webhook event."""

    RECEIVED = "RECEIVED", "Received"  # Stored but not yet dispatched.
    PROCESSED = "PROCESSED", "Processed"  # Successfully handled.
    IGNORED = "IGNORED", "Ignored"  # Valid but unmatched or unsupported.
    FAILED = "FAILED", "Failed"  # Dispatch failed after storage.


# Define Payment Audit Action values.
class PaymentAuditAction(models.TextChoices):
    """Durable action log for the gateway layer (separate from ledger postings)."""

    COLLECTION_INITIATED = "COLLECTION_INITIATED", "Collection initiated"  # Money-in request created.
    COLLECTION_CONFIRMED = "COLLECTION_CONFIRMED", "Collection confirmed"  # Receipt booked.
    COLLECTION_FAILED = "COLLECTION_FAILED", "Collection failed"  # Money-in failed or abandoned.
    VIRTUAL_ACCOUNT_CREATED = "VIRTUAL_ACCOUNT_CREATED", "Virtual account created"  # Dedicated account provisioned.
    VIRTUAL_ACCOUNT_STATUS_CHANGED = "VIRTUAL_ACCOUNT_STATUS_CHANGED", "Virtual account status changed"  # Local status flipped.
    PAYOUT_INITIATED = "PAYOUT_INITIATED", "Payout initiated"  # Money-out request created.
    PAYOUT_CONFIRMED = "PAYOUT_CONFIRMED", "Payout confirmed"  # Vendor payment booked.
    PAYOUT_FAILED = "PAYOUT_FAILED", "Payout failed"  # Money-out failed or reversed.
    PAYOUT_BATCH_CREATED = "PAYOUT_BATCH_CREATED", "Payout batch created"  # Bulk payout draft created.
    PAYOUT_BATCH_SUBMITTED = "PAYOUT_BATCH_SUBMITTED", "Payout batch submitted"  # Bulk payout sent to provider.
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED", "Webhook received"  # Valid inbound provider event stored.
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED", "Webhook rejected"  # Signature or authenticity failure.
    COLLECTIONS_SETTLED = "COLLECTIONS_SETTLED", "Collections settled to a bank"  # Clearing moved to a bank.
    SUBACCOUNT_SAVED = "SUBACCOUNT_SAVED", "Collection subaccount saved"  # Provider subaccount created or refreshed.
    CUSTODY_SETTINGS_UPDATED = "CUSTODY_SETTINGS_UPDATED", "Custody settings updated"  # Mode or interval changed.
    CUSTODY_SWITCHED = "CUSTODY_SWITCHED", "Custody mode switched"  # A pending mode took effect.
    CUSTODY_SWITCH_WAITING = "CUSTODY_SWITCH_WAITING", "Custody switch waiting"  # Due, but held money remains.
    HELD_SETTLEMENT_BUILT = "HELD_SETTLEMENT_BUILT", "Held settlement built"  # A branch's settlement payout prepared.
    HELD_SETTLEMENT_PAID = "HELD_SETTLEMENT_PAID", "Held settlement paid"  # The branch's bank received it.
    HELD_SETTLEMENT_FAILED = "HELD_SETTLEMENT_FAILED", "Held settlement failed"  # Its transfer failed.
    HELD_FUNDS_REFUSED = "HELD_FUNDS_REFUSED", "Payout refused: held funds"  # Above the branch's held balance.
    HELD_OPENING_BALANCE = "HELD_OPENING_BALANCE", "Held opening balance"  # Platform recorded money it already held.
    VIRTUAL_ACCOUNT_REISSUED = "VIRTUAL_ACCOUNT_REISSUED", "Virtual account reissued"  # Old number retired.
    PROVIDER_DISPUTE_RECEIVED = "PROVIDER_DISPUTE_RECEIVED", "Chargeback or refund received"  # Raised, not booked.


class CustodyMode(models.TextChoices):
    """Where a tenant's online money is held between the payer and its bank.

    ``DIRECT``: a provider subaccount per branch settles each payment straight to
    that branch's collection bank account, so nothing waits in a provider
    balance and the tenant makes no online payouts. ``HELD``: payments settle to
    the platform's own provider balance, and online payouts draw on it.
    """

    DIRECT = "DIRECT", "Direct to each branch's bank"
    HELD = "HELD", "Held by the platform"


class PayoutPurpose(models.TextChoices):
    """What a payout batch pays.

    ``VENDOR``: suppliers, each line naming a verified vendor. ``SETTLEMENT``: the
    platform paying a held-mode branch what it holds for it, one line into the
    branch's collection bank account (:mod:`vs_payments.held`).
    """

    VENDOR = "VENDOR", "Supplier payments"
    SETTLEMENT = "SETTLEMENT", "Settlement to a branch's bank"


class HeldMovementKind(models.TextChoices):
    """Why a held-mode branch's held balance moved (:class:`~vs_payments.models.HeldMovement`)."""

    COLLECTION = "COLLECTION", "Online payment received"  # Raises the balance by what the provider kept.
    PAYOUT = "PAYOUT", "Online payout sent"  # Lowers it when the payout is dispatched.
    SETTLEMENT = "SETTLEMENT", "Settlement to the branch's bank"  # Lowers it when the transfer is dispatched.
    RELEASE = "RELEASE", "Payout or settlement failed"  # Gives back what a failed transfer reserved.
    OPENING = "OPENING", "Opening balance"  # Money already held when the ledger started.
    DISPUTE = "DISPUTE", "Chargeback"  # A payer's bank took a held payment back.


class HeldSettlementStatus(models.TextChoices):
    """Where one branch's settlement payout stands."""

    PENDING = "PENDING", "Awaiting approval or transfer"
    PAID = "PAID", "Paid into the branch's bank"
    FAILED = "FAILED", "Transfer failed; its payments are released"


#: Settlement interval (days) a held-mode tenant is paid on, and its bounds.
DEFAULT_SETTLEMENT_INTERVAL_DAYS = 1
SETTLEMENT_INTERVAL_RANGE = (1, 7)

#: Days a confirmed collection may wait in gateway clearing before the close warns.
DEFAULT_CLEARING_STALE_DAYS = 7
CLEARING_STALE_DAYS_RANGE = (1, 60)


#: Default currency (matches the ledger default).  # Use naira by default.
DEFAULT_CURRENCY = "NGN"

#: Prefix for locally-generated provider references (our idempotency key on the way out).  # Shared outbound reference prefix.
REFERENCE_PREFIX = "CXP"


# --------------------------------------------------------------------------- #
# Payout-batch approval defaults (see vs_payments.approvals)                   #
# --------------------------------------------------------------------------- #

#: Template code for the seeded ladder. One per document type, matching procurement.
WF_DEFAULT_TEMPLATE_CODE = "standard"

#: Codes of the approver groups a tenant's two-stage payout ladder names. Created
#: empty, so a seeded stage parks until the tenant puts somebody in it.
WF_DEFAULT_APPROVE_GROUP = "payout-approver"
WF_DEFAULT_HIGH_VALUE_GROUP = "payout-senior-approver"

#: Batches at or above N500,000 require an additional, distinct senior approver.
WF_DEFAULT_HIGH_VALUE_THRESHOLD = 50_000_000

#: The platform tenant's route for a held-mode settlement batch, and the group of
#: platform finance staff any two distinct members of which approve one.
WF_SETTLEMENT_TEMPLATE_CODE = "held-settlement"
WF_SETTLEMENT_APPROVER_GROUP = "held-settlement-approver"


# The console's list screens filter by GROUP, not by raw status: one pill
# means several underlying states. Here rather than in views.py because two
# callers need the same expansion - the list endpoints, and the Export
# Centre's screen bindings, which turn "?group=PAID" back into the exact
# status set the table showed. A second copy is how an export quietly stops
# matching the screen it came from.

#: Console group -> underlying CollectionStatus values.
COLLECTION_GROUPS = {
    "PENDING": ["PENDING", "PROCESSING"],
    "PAID": ["SUCCEEDED"],
    "FAILED": ["FAILED", "ABANDONED"],
    "REFUNDED": ["REFUNDED"],
}

#: Console group -> underlying PayoutStatus values.
PAYOUT_GROUPS = {
    "PENDING": ["PENDING", "PROCESSING"],
    "PAID": ["PAID"],
    "FAILED": ["FAILED", "REVERSED"],
}
