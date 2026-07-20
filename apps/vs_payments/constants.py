"""Enumerations and well-known constants for vs_payments.  # Shared payment state and provider identifiers.

The payments app is the *gateway* layer that sits in front of the ledger: it talks to
external PSPs (Paystack) to **collect** money from customers and **pay out** money
to vendors/beneficiaries, then hands the confirmed cash movement to the existing finance
services (``vs_finance.receivables.post_payment`` for receipts;
``vs_procurement.payables.post_vendor_payment`` for payouts). Money is always integer
**kobo** here too — never float.  # Keep gateway values aligned with the ledger.
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
    receipt); ``FAILED``/``ABANDONED`` → terminal, no money; ``REFUNDED`` → reversed.
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


# Define Virtual Account Status values.
class VirtualAccountStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"  # Available for incoming transfers.
    INACTIVE = "INACTIVE", "Inactive"  # No longer offered for new transfers.


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


#: Default currency (matches the ledger default).  # Use naira by default.
DEFAULT_CURRENCY = "NGN"

#: Prefix for locally-generated provider references (our idempotency key on the way out).  # Shared outbound reference prefix.
REFERENCE_PREFIX = "CXP"
