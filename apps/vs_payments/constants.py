"""Enumerations and well-known constants for vs_payments.

The payments app is the *gateway* layer that sits in front of the ledger: it talks to
external PSPs (OPay, Paystack) to **collect** money from customers and **pay out** money
to vendors/beneficiaries, then hands the confirmed cash movement to the existing finance
services (``vs_finance.receivables.post_payment`` for receipts;
``vs_procurement.payables.post_vendor_payment`` for payouts). Money is always integer
**kobo** here too — never float.
"""
from __future__ import annotations

from django.db import models


class PaymentProvider(models.TextChoices):
    """The external payment service providers we integrate with."""

    OPAY = "OPAY", "OPay"
    PAYSTACK = "PAYSTACK", "Paystack"
    FAKE = "FAKE", "Fake (testing)"


class PaymentDirection(models.TextChoices):
    """Which way money flows relative to the ledger entity."""

    COLLECTION = "COLLECTION", "Collection (money in)"
    PAYOUT = "PAYOUT", "Payout (money out)"


class CollectionChannel(models.TextChoices):
    """How a collection is presented to the payer."""

    CHECKOUT = "CHECKOUT", "Hosted checkout / redirect"
    VIRTUAL_ACCOUNT = "VIRTUAL_ACCOUNT", "Dedicated virtual account (NUBAN)"
    CARD = "CARD", "Card"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
    USSD = "USSD", "USSD"


class CollectionStatus(models.TextChoices):
    """Lifecycle of a collection intent.

    ``PENDING`` → created locally, payer not yet acted; ``PROCESSING`` → provider
    acknowledged/awaiting settlement; ``SUCCEEDED`` → confirmed paid (terminal, books a
    receipt); ``FAILED``/``ABANDONED`` → terminal, no money; ``REFUNDED`` → reversed.
    """

    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    ABANDONED = "ABANDONED", "Abandoned"
    REFUNDED = "REFUNDED", "Refunded"


#: Collection states past which no further automatic transition happens.
COLLECTION_TERMINAL = frozenset(
    {CollectionStatus.SUCCEEDED, CollectionStatus.FAILED,
     CollectionStatus.ABANDONED, CollectionStatus.REFUNDED}
)


class PayoutStatus(models.TextChoices):
    """Lifecycle of a payout instruction (money leaving the entity)."""

    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    PAID = "PAID", "Paid"
    FAILED = "FAILED", "Failed"
    REVERSED = "REVERSED", "Reversed"


#: Payout states past which no further automatic transition happens.
PAYOUT_TERMINAL = frozenset(
    {PayoutStatus.PAID, PayoutStatus.FAILED, PayoutStatus.REVERSED}
)


class PayoutBatchStatus(models.TextChoices):
    """Lifecycle of a bulk-disbursement batch grouping many payout instructions.

    ``DRAFT`` → created locally with child instructions but not yet submitted;
    ``PROCESSING`` → submitted, at least one child accepted by the provider and none
    finished failing; ``COMPLETED`` → every child reached ``PAID``;
    ``PARTIALLY_COMPLETED`` → batch finished but a mix of paid/failed children;
    ``FAILED`` → every child failed to submit/settle.
    """

    DRAFT = "DRAFT", "Draft"
    PROCESSING = "PROCESSING", "Processing"
    COMPLETED = "COMPLETED", "Completed"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED", "Partially completed"
    FAILED = "FAILED", "Failed"


#: Batch states past which no further automatic transition happens.
PAYOUT_BATCH_TERMINAL = frozenset(
    {PayoutBatchStatus.COMPLETED, PayoutBatchStatus.PARTIALLY_COMPLETED,
     PayoutBatchStatus.FAILED}
)


class VirtualAccountStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"


class WebhookStatus(models.TextChoices):
    """Processing state of a raw inbound webhook event."""

    RECEIVED = "RECEIVED", "Received"
    PROCESSED = "PROCESSED", "Processed"
    IGNORED = "IGNORED", "Ignored"
    FAILED = "FAILED", "Failed"


class PaymentAuditAction(models.TextChoices):
    """Durable action log for the gateway layer (separate from ledger postings)."""

    COLLECTION_INITIATED = "COLLECTION_INITIATED", "Collection initiated"
    COLLECTION_CONFIRMED = "COLLECTION_CONFIRMED", "Collection confirmed"
    COLLECTION_FAILED = "COLLECTION_FAILED", "Collection failed"
    VIRTUAL_ACCOUNT_CREATED = "VIRTUAL_ACCOUNT_CREATED", "Virtual account created"
    PAYOUT_INITIATED = "PAYOUT_INITIATED", "Payout initiated"
    PAYOUT_CONFIRMED = "PAYOUT_CONFIRMED", "Payout confirmed"
    PAYOUT_FAILED = "PAYOUT_FAILED", "Payout failed"
    PAYOUT_BATCH_CREATED = "PAYOUT_BATCH_CREATED", "Payout batch created"
    PAYOUT_BATCH_SUBMITTED = "PAYOUT_BATCH_SUBMITTED", "Payout batch submitted"
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED", "Webhook received"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED", "Webhook rejected"


#: Default currency (matches the ledger default).
DEFAULT_CURRENCY = "NGN"

#: Prefix for locally-generated provider references (our idempotency key on the way out).
REFERENCE_PREFIX = "CXP"
