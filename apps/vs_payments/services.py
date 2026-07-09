"""Orchestration between the external PSP gateway and the ledger.

This is where a confirmed *gateway* event becomes an authoritative *ledger* posting:

* a confirmed **collection** books a ``vs_finance.Payment`` receipt (Dr bank, Cr AR) via
  ``vs_finance.receivables.post_payment``;
* a confirmed **payout** books a ``vs_procurement.VendorPayment`` (Dr AP, Cr bank, Cr WHT)
  via ``vs_procurement.payables.post_vendor_payment``.

Every confirm path is **idempotent** (``select_for_update`` on the gateway row + a
terminal-state short-circuit) so a provider retrying a webhook can never post twice.
Amounts stay integer **kobo** throughout.
"""
from __future__ import annotations  # Keep postponed annotation evaluation for type hints.

import datetime  # Used for posting dates on ledger documents.
import uuid  # Used to generate unique gateway references.

from django.db import transaction  # Wrap state transitions that must be atomic.
from django.utils import timezone  # Timestamp confirmations with the application timezone.
from rest_framework.exceptions import ValidationError  # Surface request-style validation errors.

from vs_finance.accounts import resolve_account  # Resolve the default cash/bank account when needed.
from vs_finance.constants import CASH_BANK_CODE, PaymentMethod  # Shared finance constants.
from vs_finance.exceptions import FinanceError  # Provider and finance integration failures.

from . import audit  # Emit payment audit events for every state transition.
from .constants import (
    CollectionStatus,
    PaymentAuditAction,
    PaymentProvider,
    PayoutBatchStatus,
    PayoutStatus,
    REFERENCE_PREFIX,
    VirtualAccountStatus,
)
from .exceptions import PaymentStateError  # Raised when payment state does not allow the requested action.
from .models import CollectionIntent, PayoutBatch, PayoutInstruction, VirtualAccount  # Payment domain models.
from .providers.registry import get_provider  # Look up the configured PSP adapter.


def _new_reference() -> str:
    """A unique merchant reference / idempotency key for an outbound request."""
    return f"{REFERENCE_PREFIX}-{uuid.uuid4().hex[:20].upper()}"  # Prefix plus random suffix keeps references readable.


def _entity_currency(entity):
    return getattr(entity, "base_currency", None)  # Prefer the entity's configured base currency.


# --------------------------------------------------------------------------- #
# Collections (money in)                                                       #
# --------------------------------------------------------------------------- #

def initiate_collection(*, entity, amount, customer=None, invoice=None,
                        deposit_account=None, channel=None, provider=None,
                        payer_email="", payer_name="", narration="", currency=None,
                        callback_url=None, metadata=None, actor_user=None):
    """Create a :class:`CollectionIntent` and ask the provider to start a collection.

    Returns the intent with ``checkout_url`` (and ``provider_reference``) populated. No
    ledger entry is made yet — the receipt is booked only when the collection is
    *confirmed* (webhook or verify).
    """
    from .constants import CollectionChannel  # Import lazily to avoid circular module loading.

    from django.conf import settings  # Read payment defaults from Django settings.

    channel = channel or CollectionChannel.CHECKOUT  # Default to a checkout-style collection.
    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Fall back to the configured PSP.
    client = get_provider(provider_name)  # Resolve the PSP client once for this request.
    reference = _new_reference()  # Generate a unique reference for the provider and our ledger.
    callback_url = callback_url or getattr(settings, "PAYMENTS_CALLBACK_URL", "")  # Use the configured callback URL if none is provided.
    currency = currency or _entity_currency(entity)  # Keep the collection in the entity's currency by default.
    if invoice is not None and customer is None:  # Allow invoice-driven collections to infer the customer.
        customer = invoice.customer  # Pull the customer from the invoice when possible.
    if customer is None:  # The receipt cannot be posted without a customer AR account.
        raise ValidationError({"customer": "A customer is required to book the collection receipt."})

    intent = CollectionIntent.objects.create(  # Persist the local intent before calling the provider.
        entity=entity, provider=provider_name, channel=channel, reference=reference,
        amount=amount, currency=currency, customer=customer, invoice=invoice,
        deposit_account=deposit_account, payer_email=payer_email or
        (customer.billing_email if customer else ""),
        payer_name=payer_name or (customer.name if customer else ""),
        narration=narration, metadata=metadata or {}, created_by=actor_user,
        status=CollectionStatus.PENDING,
    )

    try:  # Provider calls can fail independently from local validation.
        result = client.create_checkout(
            reference=reference, amount=amount,
            currency=getattr(currency, "code", currency) or "NGN",
            customer_email=intent.payer_email, customer_name=intent.payer_name,
            narration=narration, callback_url=callback_url, metadata=metadata or {},
        )
    except FinanceError as exc:  # Mirror provider failure locally so retries see the correct terminal state.
        intent.status = CollectionStatus.FAILED  # Mark the intent failed when checkout creation is rejected.
        intent.raw_response = {"error": str(getattr(exc, "message", exc))}  # Persist the provider error for debugging.
        intent.save(update_fields=["status", "raw_response", "updated_at"])  # Save only the fields that changed.
        audit.record_rejection(
            action=PaymentAuditAction.COLLECTION_INITIATED, exc=exc, entity=entity,
            provider=provider_name, reference=reference, actor_user=actor_user,
        )
        raise

    intent.provider_reference = result.provider_reference  # Store the PSP-side identifier for later verification.
    intent.checkout_url = result.checkout_url  # Expose the hosted checkout URL to the caller.
    intent.authorization_code = result.authorization_code  # Keep any immediate authorization token.
    intent.status = CollectionStatus.PROCESSING  # The provider accepted the request, but money is not confirmed yet.
    intent.raw_response = result.raw  # Preserve the raw PSP response for audit and support.
    intent.save(update_fields=[
        "provider_reference", "checkout_url", "authorization_code", "status",
        "raw_response", "updated_at",
    ])
    audit.record(  # Emit a single audit event for the successful initiation.
        action=PaymentAuditAction.COLLECTION_INITIATED, entity=entity,
        provider=provider_name, reference=reference, actor_user=actor_user,
        message=f"Initiated {amount} kobo collection via {provider_name}.",
        metadata={"channel": channel},
    )
    return intent  # Return the hydrated intent to the caller.


def create_virtual_account(*, entity, customer, provider=None, deposit_account=None,
                           bank_code="", actor_user=None):
    """Provision a dedicated virtual NUBAN for ``customer`` and store it."""
    from django.conf import settings  # Read the default PSP from settings.

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Resolve the PSP to use.
    if VirtualAccount.objects.filter(  # Avoid creating duplicate active virtual accounts for the same customer.
        entity=entity, provider=provider_name, customer=customer,
        status=VirtualAccountStatus.ACTIVE,
    ).exists():
        raise ValidationError(  # Fail fast so the caller can choose whether to reuse or deactivate first.
            {"customer": "This customer already has an active virtual account with this provider."})
    client = get_provider(provider_name)  # Reuse the configured PSP client.
    reference = _new_reference()  # Give the PSP request its own reference.
    result = client.create_virtual_account(  # Ask the PSP to provision the account.
        reference=reference, customer_name=customer.name,
        customer_email=customer.billing_email, bank_code=bank_code,
    )
    va = VirtualAccount.objects.create(  # Store the provider-issued account details locally.
        entity=entity, provider=provider_name, customer=customer,
        deposit_account=deposit_account, account_number=result.account_number,
        bank_name=result.bank_name, account_name=result.account_name,
        currency=_entity_currency(entity), provider_reference=result.provider_reference,
        status=VirtualAccountStatus.ACTIVE, raw=result.raw,
    )
    audit.record(  # Record the new virtual account for traceability.
        action=PaymentAuditAction.VIRTUAL_ACCOUNT_CREATED, entity=entity,
        provider=provider_name, reference=reference, actor_user=actor_user,
        message=f"Virtual account {result.account_number} for {customer.code}.",
    )
    return va  # Return the stored model instance.


@transaction.atomic
def set_virtual_account_status(va, *, status, actor_user=None):
    """Activate or deactivate a virtual account on our side.

    We flip the local status and record it. Provider-side teardown is **not**
    wired (no provider method backs it), so a deactivated account stops being
    offered for new transfers here while remaining whatever it is at the PSP.
    """
    if status not in VirtualAccountStatus.values:  # Reject invalid lifecycle states.
        raise ValidationError({"status": f"Must be one of {', '.join(VirtualAccountStatus.values)}."})
    if va.status == status:  # No work to do when the requested state is already applied.
        return va
    va.status = status  # Update the local record only.
    va.save()  # Persist the status change.
    audit.record(  # Write an audit event so the status flip is visible later.
        action=PaymentAuditAction.VIRTUAL_ACCOUNT_STATUS_CHANGED, entity=va.entity,
        provider=va.provider, reference=va.provider_reference, actor_user=actor_user,
        message=f"Virtual account {va.account_number} set to {status}.",
    )
    return va  # Return the updated virtual account.


@transaction.atomic
def confirm_collection(intent, *, status=None, amount=None, actor_user=None):
    """Confirm a collection and book the receipt — idempotently.

    ``status`` (a :class:`CollectionStatus` value) is taken from a webhook/verify result;
    if omitted, the provider is polled. A SUCCEEDED collection books a customer receipt
    (Dr bank, Cr AR) and links it; FAILED/ABANDONED is recorded with no ledger effect.
    Re-confirming an already-terminal intent is a no-op (returns it unchanged).
    """
    intent = CollectionIntent.objects.select_for_update().get(pk=intent.pk)  # Lock the row for idempotent confirmation.
    if intent.is_terminal:  # Terminal rows are already settled or failed.
        return intent  # Exit without duplicating ledger work.

    if status is None:  # When no explicit status is supplied, verify with the PSP.
        client = get_provider(intent.provider)  # Resolve the provider using the stored intent value.
        result = client.verify_collection(  # Ask the PSP for the final collection state.
            reference=intent.reference, provider_reference=intent.provider_reference,
        )
        status = result.status  # Trust the PSP status for the confirmation decision.
        amount = result.amount or intent.amount  # Fall back to the original amount if the PSP omits it.
        intent.raw_response = {**(intent.raw_response or {}), "verify": result.raw}  # Append the verification payload.

    if status != CollectionStatus.SUCCEEDED:  # Only success books a receipt.
        intent.status = (CollectionStatus.FAILED if status == CollectionStatus.FAILED
                         else CollectionStatus.ABANDONED if status == CollectionStatus.ABANDONED
                         else intent.status)
        intent.save(update_fields=["status", "raw_response", "updated_at"])  # Save the terminal non-success state.
        audit.record(  # Capture the failure path for audit visibility.
            action=PaymentAuditAction.COLLECTION_FAILED, entity=intent.entity,
            provider=intent.provider, reference=intent.reference, succeeded=False,
            message=f"Collection ended '{status}'.", actor_user=actor_user,
        )
        return intent  # Stop here because failed collections have no ledger effect.

    settled = amount or intent.amount  # Use the confirmed amount when the PSP returns one.
    if settled > 0 and settled != intent.amount:  # Preserve the originally requested amount in metadata.
        # Book the amount that actually cleared, but retain the requested value for audit.
        intent.metadata = {**(intent.metadata or {}), "requested_amount": intent.amount}  # Store the pre-settlement amount.
        intent.amount = settled  # Replace the receipt amount with the actual settled amount.

    _book_receipt(intent, actor_user=actor_user)  # Create and post the corresponding receipt.
    intent.status = CollectionStatus.SUCCEEDED  # Mark the gateway event as settled.
    intent.confirmed_at = timezone.now()  # Record the confirmation timestamp.
    intent.save(update_fields=[
        "status", "payment", "amount", "metadata", "confirmed_at", "raw_response", "updated_at",
    ])
    audit.record(  # Emit a success audit event with the linked payment id.
        action=PaymentAuditAction.COLLECTION_CONFIRMED, entity=intent.entity,
        provider=intent.provider, reference=intent.reference, actor_user=actor_user,
        message=f"Booked receipt for {intent.amount} kobo.",
        metadata={"payment_id": intent.payment_id},
    )
    return intent  # Return the confirmed collection intent.


def _book_receipt(intent, *, actor_user=None):
    """Create + post the ``vs_finance.Payment`` for a succeeded collection."""
    from vs_finance.models import Payment  # Create the cash receipt record.
    from vs_finance.receivables import post_payment  # Allocate the receipt into receivables.

    if (intent.virtual_account_id  # If the receipt landed on a virtual account...
            and intent.virtual_account.status == VirtualAccountStatus.INACTIVE):  # ...and that account is inactive...
        raise PaymentStateError(  # ...then stop and send it to manual review.
            "Virtual account is inactive; deposit held for manual review.")
    if intent.customer_id is None:  # Receipts need a customer so receivables can be posted correctly.
        raise PaymentStateError(
            "Cannot book a receipt: the collection has no customer (AR sub-ledger).",
        )
    deposit = intent.deposit_account or resolve_account(  # Use the configured cash/bank account when none was provided.
        intent.entity, CASH_BANK_CODE, label="Cash & bank",
    )
    payment = Payment.objects.create(  # Build the customer payment before applying allocations.
        entity=intent.entity, customer=intent.customer,
        payment_date=datetime.date.today(), currency=intent.currency,
        method=PaymentMethod.ONLINE, amount=intent.amount, deposit_account=deposit,
        reference=intent.reference,
        narration=intent.narration or f"Gateway collection {intent.reference}",
    )
    if intent.invoice_id:  # Invoice-linked receipts should settle that invoice directly.
        post_payment(payment, actor_user=actor_user,
                     allocations=[(intent.invoice, intent.amount)])  # Allocate the full settled amount to the invoice.
    else:  # Standalone receipts should not guess at invoice allocation.
        # Standalone receipt: leave the funds as customer credit instead of auto-allocating them.
        post_payment(payment, actor_user=actor_user, auto_allocate=False)  # Park the money as credit instead.
    intent.payment = payment  # Link the payment back to the gateway record.


# --------------------------------------------------------------------------- #
# Payouts (money out)                                                          #
# --------------------------------------------------------------------------- #

def initiate_payout(*, entity, amount, beneficiary_name, beneficiary_account_number,
                    beneficiary_bank_code, vendor=None, source_account=None,
                    provider=None, narration="", currency=None, wht_amount=0,
                    metadata=None, actor_user=None):
    """Create a :class:`PayoutInstruction` and ask the provider to transfer funds out.

    The ledger entry (a vendor payment) is booked only on *confirmation*. If ``vendor``
    is given it is recorded as a loose reference so confirm can re-resolve and book a
    ``VendorPayment``; the WHT split (``wht_amount``) flows through to that posting.
    """
    from django.conf import settings  # Read the configured provider from Django settings.

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Resolve the PSP to use.
    client = get_provider(provider_name)  # Prepare the transfer client up front.
    reference = _new_reference()  # Assign a unique payout reference.
    currency = currency or _entity_currency(entity)  # Default to the entity currency for outbound transfers.

    payout = PayoutInstruction.objects.create(  # Persist the payout before contacting the provider.
        entity=entity, provider=provider_name, reference=reference, amount=amount,
        currency=currency, beneficiary_name=beneficiary_name,
        beneficiary_account_number=beneficiary_account_number,
        beneficiary_bank_code=beneficiary_bank_code, source_account=source_account,
        narration=narration, status=PayoutStatus.PENDING,
        vendor_source_type="vs_procurement.Vendor" if vendor else "",
        vendor_source_id=str(vendor.pk) if vendor else "",
        metadata={**(metadata or {}), "wht_amount": int(wht_amount)},
        created_by=actor_user,
    )
    return _dispatch_transfer(payout, client=client, metadata=metadata, actor_user=actor_user)  # Submit the payout immediately.


def _dispatch_transfer(payout, *, client=None, metadata=None, actor_user=None):
    """Ask the provider to transfer funds for an already-created ``PENDING`` payout.

    Shared by single :func:`initiate_payout` and bulk :func:`submit_payout_batch`. On
    provider rejection the payout is marked FAILED and the error re-raised; on success it
    moves to PROCESSING with the provider's reference/recipient stored. Booking the ledger
    entry still happens later, on confirmation.
    """
    client = client or get_provider(payout.provider)  # Allow callers to reuse or lazily resolve the PSP client.
    currency = payout.currency  # Store the payout currency once for the request.
    try:  # Transfer creation can fail independently from local persistence.
        result = client.create_transfer(
            reference=payout.reference, amount=payout.amount,
            currency=getattr(currency, "code", currency) or "NGN",
            account_number=payout.beneficiary_account_number,
            bank_code=payout.beneficiary_bank_code,
            account_name=payout.beneficiary_name, narration=payout.narration,
            metadata=metadata or {},
        )
    except FinanceError as exc:  # Keep the local payout row in sync with the provider failure.
        payout.status = PayoutStatus.FAILED  # Mark the payout as failed locally.
        payout.failure_reason = str(getattr(exc, "message", exc))[:255]  # Store a short human-readable failure reason.
        payout.save(update_fields=["status", "failure_reason", "updated_at"])  # Persist only the failure fields.
        audit.record_rejection(  # Emit a rejection event for the failed payout request.
            action=PaymentAuditAction.PAYOUT_INITIATED, exc=exc, entity=payout.entity,
            provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        )
        raise  # Re-raise so the caller can surface the provider error.

    payout.provider_reference = result.provider_reference  # Store the provider's transaction reference.
    payout.recipient_code = result.recipient_code  # Keep the provider recipient code for later verification.
    payout.status = PayoutStatus.PROCESSING  # The transfer is now in flight.
    payout.raw_response = result.raw  # Persist the provider response payload.
    payout.save(update_fields=[
        "provider_reference", "recipient_code", "status", "raw_response", "updated_at",
    ])
    audit.record(  # Capture the successful provider submission.
        action=PaymentAuditAction.PAYOUT_INITIATED, entity=payout.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        message=f"Initiated {payout.amount} kobo payout via {payout.provider}.",
    )
    return payout  # Return the now-processing payout instruction.


# --------------------------------------------------------------------------- #
# Bulk payouts (provider bulk submit)                                          #
# --------------------------------------------------------------------------- #

def create_payout_batch(*, entity, items, provider=None, source_account=None,
                        title="", narration="", currency=None, actor_user=None):
    """Assemble a :class:`PayoutBatch` plus its child ``PENDING`` instructions (no submit).

    ``items`` is an iterable of dicts, each with ``amount`` (kobo) and beneficiary fields
    (``beneficiary_name``, ``beneficiary_account_number``, ``beneficiary_bank_code``) and
    optional ``vendor`` / ``narration`` / ``wht_amount`` / ``metadata`` / ``source_account``.
    Nothing is sent to the provider yet — call :func:`submit_payout_batch` for that.
    """
    from django.conf import settings  # Pull provider defaults from configuration.

    items = list(items)  # Materialize the iterable so it can be counted and iterated safely.
    if not items:  # A batch with no items is not meaningful.
        raise PaymentStateError("A payout batch must contain at least one item.")

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Resolve the batch PSP.
    get_provider(provider_name)  # Validate the provider configuration before creating batch rows.
    currency = currency or _entity_currency(entity)  # Default the batch currency to the entity currency.
    batch_reference = _new_reference()  # Use one reference for the whole batch.

    with transaction.atomic():  # Make batch and child instruction creation all-or-nothing.
        batch = PayoutBatch.objects.create(  # Create the parent batch record first.
            entity=entity, provider=provider_name, reference=batch_reference,
            title=title, narration=narration, currency=currency,
            source_account=source_account, status=PayoutBatchStatus.DRAFT,
            created_by=actor_user,
        )
        total = 0  # Accumulate the batch total as each instruction is added.
        for item in items:  # Each dict becomes one payout instruction.
            amount = int(item.get("amount") or 0)  # Normalize the amount to an integer kobo value.
            if amount <= 0:  # Reject empty or negative payout lines.
                raise PaymentStateError("Each payout item needs a positive amount (kobo).")
            vendor = item.get("vendor")  # Preserve the vendor link when one is supplied.
            PayoutInstruction.objects.create(  # Create the child instruction in pending state.
                entity=entity, batch=batch, provider=provider_name,
                reference=_new_reference(), amount=amount, currency=currency,
                beneficiary_name=item["beneficiary_name"],
                beneficiary_account_number=item["beneficiary_account_number"],
                beneficiary_bank_code=item.get("beneficiary_bank_code", ""),
                source_account=item.get("source_account") or source_account,
                narration=item.get("narration", "") or narration,
                status=PayoutStatus.PENDING,
                vendor_source_type="vs_procurement.Vendor" if vendor else "",
                vendor_source_id=str(vendor.pk) if vendor else "",
                metadata={**(item.get("metadata") or {}),  # Preserve any caller-supplied metadata...
                          "wht_amount": int(item.get("wht_amount") or 0)},  # ...while always storing WHT explicitly.
                created_by=actor_user,
            )
            total += amount  # Keep the running batch total in sync.
        batch.total_amount = total  # Store the aggregate amount on the batch.
        batch.item_count = len(items)  # Store the number of instructions on the batch.
        batch.save(update_fields=["total_amount", "item_count", "updated_at"])  # Persist the aggregate fields only.

    audit.record(  # Write a batch-level audit event after the transaction commits.
        action=PaymentAuditAction.PAYOUT_BATCH_CREATED, entity=entity,
        provider=provider_name, reference=batch_reference, actor_user=actor_user,
        message=f"Created payout batch of {len(items)} items, {total} kobo.",
    )
    return batch  # Return the draft batch for later submission.


def submit_payout_batch(batch, *, actor_user=None):
    """Submit every ``PENDING`` instruction in ``batch`` to the provider, one by one.

    Each item rides the shared :func:`_dispatch_transfer`; a per-item provider rejection
    marks that instruction FAILED but does not abort the run. The batch's aggregate status
    is recomputed from its children afterwards. Idempotent: already-dispatched items
    (non-PENDING) are skipped, so re-submitting a partially-failed batch only retries the
    stragglers.
    """
    submitted = failed = 0  # Track how many instructions were accepted or rejected.
    for payout in batch.instructions.filter(status=PayoutStatus.PENDING):  # Only pending items are eligible for submission.
        try:  # One failed instruction should not abort the whole batch.
            _dispatch_transfer(payout, actor_user=actor_user)  # Submit this payout to the provider.
            submitted += 1  # Count successful submissions.
        except FinanceError:  # Keep going so later rows still have a chance to submit.
            failed += 1  # Count provider rejections.

    batch.submitted_at = batch.submitted_at or timezone.now()  # Set the first submission timestamp once.
    _recompute_batch_status(batch)  # Recalculate the batch status from its children.
    audit.record(  # Emit the batch submission audit event with the outcome counts.
        action=PaymentAuditAction.PAYOUT_BATCH_SUBMITTED, entity=batch.entity,
        provider=batch.provider, reference=batch.reference, actor_user=actor_user,
        message=f"Submitted batch: {submitted} dispatched, {failed} failed.",
        metadata={"submitted": submitted, "failed": failed},
    )
    return batch  # Return the batch after aggregate status refresh.


def _recompute_batch_status(batch):
    """Derive and persist the batch status from the live state of its instructions."""
    statuses = list(  # Pull the child statuses into memory for aggregation.
        batch.instructions.values_list("status", flat=True)
    )
    total = len(statuses)  # Total number of instructions in the batch.
    paid = sum(1 for s in statuses if s == PayoutStatus.PAID)  # Count settled instructions.
    failed = sum(1 for s in statuses if s in (PayoutStatus.FAILED, PayoutStatus.REVERSED))  # Count terminal failures.
    pending = sum(1 for s in statuses if s == PayoutStatus.PENDING)  # Count rows not yet sent.
    in_flight = sum(1 for s in statuses if s == PayoutStatus.PROCESSING)  # Count rows waiting on PSP confirmation.

    if total == 0:  # Empty batches stay in draft.
        status = PayoutBatchStatus.DRAFT
    elif pending == total:  # A batch with only pending instructions has not started yet.
        status = PayoutBatchStatus.DRAFT
    elif paid == total:  # All children paid means the batch is complete.
        status = PayoutBatchStatus.COMPLETED
    elif failed == total:  # All children failed means the batch failed overall.
        status = PayoutBatchStatus.FAILED
    elif pending or in_flight:  # Mixed pending or in-flight rows means work is still ongoing.
        status = PayoutBatchStatus.PROCESSING
    else:  # A mixed paid/failed outcome with no work left.
        # Everything settled, but a mix of paid and failed.
        status = PayoutBatchStatus.PARTIALLY_COMPLETED

    if batch.status != status or batch.submitted_at is not None:  # Persist the recomputed status when it changed or after first submit.
        batch.status = status  # Store the aggregate status.
        batch.save(update_fields=["status", "submitted_at", "updated_at"])  # Save the batch timestamps and status together.
    return batch  # Return the refreshed batch.


@transaction.atomic
def confirm_payout(payout, *, status=None, actor_user=None):
    """Confirm a payout and book the vendor payment — idempotently."""
    payout = PayoutInstruction.objects.select_for_update().get(pk=payout.pk)  # Lock the payout row before confirming it.
    if payout.is_terminal:  # Already confirmed or failed rows should not be processed again.
        return payout  # Exit early for idempotency.

    if status is None:  # Ask the PSP when the caller did not provide a terminal status.
        client = get_provider(payout.provider)  # Resolve the correct provider adapter.
        result = client.verify_transfer(  # Fetch the current transfer state from the PSP.
            reference=payout.reference, provider_reference=payout.provider_reference,
        )
        status = result.status  # Use the provider's transfer status for confirmation.
        payout.raw_response = {**(payout.raw_response or {}), "verify": result.raw}  # Append the verification payload.

    if status != PayoutStatus.PAID:  # Only a paid transfer can book a vendor payment.
        if status in (PayoutStatus.FAILED, PayoutStatus.REVERSED):  # Preserve only terminal negative outcomes locally.
            payout.status = status  # Mirror the final failure state.
        payout.save(update_fields=["status", "raw_response", "updated_at"])  # Persist the non-paid result.
        audit.record(  # Record the failed payout confirmation for auditability.
            action=PaymentAuditAction.PAYOUT_FAILED, entity=payout.entity,
            provider=payout.provider, reference=payout.reference, succeeded=False,
            message=f"Payout ended '{status}'.", actor_user=actor_user,
        )
        _refresh_batch(payout)  # Keep the parent batch aggregate in sync.
        return payout  # Stop because no vendor payment should be posted.

    _book_vendor_payment(payout, actor_user=actor_user)  # Post the vendor payment into the ledger.
    payout.status = PayoutStatus.PAID  # Mark the payout as successfully settled.
    payout.confirmed_at = timezone.now()  # Capture the confirmation timestamp.
    payout.save(update_fields=[
        "status", "vendor_payment_id", "confirmed_at", "raw_response", "updated_at",
    ])
    audit.record(  # Emit the successful confirmation audit event.
        action=PaymentAuditAction.PAYOUT_CONFIRMED, entity=payout.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        message=f"Booked vendor payment for {payout.amount} kobo.",
        metadata={"vendor_payment_id": payout.vendor_payment_id},
    )
    _refresh_batch(payout)  # Refresh the parent batch after the child status changes.
    return payout  # Return the confirmed payout instruction.


def _refresh_batch(payout):
    """Recompute the owning batch's aggregate status after a child changed, if any."""
    if payout.batch_id:  # Only child payouts inside a batch need refresh work.
        _recompute_batch_status(
            PayoutBatch.objects.select_for_update().get(pk=payout.batch_id)
        )


def _book_vendor_payment(payout, *, actor_user=None):
    """Create + post the ``vs_procurement.VendorPayment`` for a paid payout."""
    if not payout.vendor_source_id:  # Vendor-backed payouts need a source reference to post AP correctly.
        raise PaymentStateError(
            "Cannot book a vendor payment: the payout has no vendor reference.",
        )
    from vs_procurement.models import Vendor, VendorPayment  # Import procurement models only when needed.
    from vs_procurement.payables import post_vendor_payment  # Post the vendor payment into payables.

    vendor = Vendor.objects.get(pk=int(payout.vendor_source_id))  # Re-resolve the vendor from the stored reference.
    wht = int((payout.metadata or {}).get("wht_amount", 0))  # Pull the withheld tax amount out of metadata.
    vp = VendorPayment.objects.create(  # Create the vendor payment record before ledger posting.
        entity=payout.entity, vendor=vendor, payment_date=datetime.date.today(),
        currency=payout.currency, method=PaymentMethod.BANK_TRANSFER,
        gross_amount=payout.amount, wht_amount=wht,
        net_amount=payout.amount - wht,
        payment_account=payout.source_account or resolve_account(
            payout.entity, CASH_BANK_CODE, label="Cash & bank",
        ),
        reference=payout.reference,
        narration=payout.narration or f"Gateway payout {payout.reference}",
    )
    post_vendor_payment(vp, actor_user=actor_user)  # Post the AP movement for the paid payout.
    payout.vendor_payment_id = vp.pk  # Link the payout instruction to the vendor payment.
