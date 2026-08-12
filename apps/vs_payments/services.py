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
from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_finance.accounts import resolve_account
from vs_finance.constants import CASH_BANK_CODE, PaymentMethod
from vs_finance.exceptions import FinanceError

from . import audit
from .constants import (
    CollectionChannel,
    CollectionStatus,
    PaymentAuditAction,
    PaymentProvider,
    PayoutBatchStatus,
    PayoutStatus,
    REFERENCE_PREFIX,
    VirtualAccountStatus,
)
from .exceptions import PaymentStateError
from .models import CollectionIntent, PayoutBatch, PayoutInstruction, VirtualAccount
from .providers.registry import get_provider


# Support the new reference workflow.
def _new_reference(entity) -> str:
    """Allocate a tenant-level daily merchant reference for an outbound request."""
    from vs_tenants.numbering import next_tenant_document_number

    return next_tenant_document_number(
        tenant=entity.tenant, document_code=REFERENCE_PREFIX,
    )


# Support the entity currency workflow.
def _entity_currency(entity):
    return getattr(entity, "base_currency", None)  # Prefer the entity's configured base currency.


# --------------------------------------------------------------------------- #
# Collections (money in)                                                       #
# --------------------------------------------------------------------------- #

# Handle the initiate collection workflow.
def initiate_collection(*, entity, amount, customer=None, invoice=None,
                        deposit_account=None, channel=None, provider=None,
                        payer_email="", payer_name="", narration="", currency=None,
                        callback_url=None, metadata=None, actor_user=None):
    """Create a :class:`CollectionIntent` and ask the provider to start a collection.

    Returns the intent with ``checkout_url`` (and ``provider_reference``) populated. No
    ledger entry is made yet - the receipt is booked only when the collection is
    *confirmed* (webhook or verify).
    """
    from django.conf import settings

    channel = channel or CollectionChannel.CHECKOUT  # Default to a checkout-style collection.
    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Fall back to the configured PSP.
    client = get_provider(provider_name)  # Resolve the PSP client once for this request.
    reference = _new_reference(entity)  # Generate a unique reference for the provider and our ledger.
    callback_url = callback_url or getattr(settings, "PAYMENTS_CALLBACK_URL", "")  # Use the configured callback URL if none is provided.
    currency = currency or _entity_currency(entity)  # Keep the collection in the entity's currency by default.

    if invoice is not None and customer is None:  # Allow invoice-driven collections to infer the customer.
        customer = invoice.customer  # Pull the customer from the invoice when possible.

    if customer is None:  # The receipt cannot be posted without a customer AR account.
        raise ValidationError({"customer": "A customer is required to book the collection receipt."})
    
    if invoice is not None:
        if invoice.customer_id != customer.id:
            raise ValidationError({"invoice": "The invoice must belong to the selected customer."})
        if invoice.status != "POSTED" or invoice.balance_due <= 0:
            raise ValidationError({"invoice": "Select a posted invoice with an outstanding balance."})
        if amount > invoice.balance_due:
            raise ValidationError({"amount": "The collection cannot exceed the invoice balance."})

    intent = CollectionIntent.objects.create(
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
        intent.save(update_fields=["status", "raw_response", "updated_at"])
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


# Handle the record virtual account deposit workflow.
def record_virtual_account_deposit(*, virtual_account, reference, amount,
                                   provider_reference="", event_type=""):
    """Materialise the :class:`CollectionIntent` for a transfer paid into one of our NUBANs.

    A dedicated virtual account exists precisely so a payer can transfer money *without*
    a checkout, which means there is no local intent for the webhook to match: the money
    simply lands. This is the missing half of that flow - given the account the provider
    says was credited, it creates the collection that account's customer would have had,
    so the deposit then rides the ordinary confirm/book path
    (:func:`confirm_collection` re-verifies with the provider, ``_book_receipt`` posts
    the receipt) instead of being dropped as an unmatched event.

    ``reference`` is the provider's own transaction reference for the deposit and becomes
    our ``reference``, which does three jobs at once: it is what
    ``verify_collection`` is polled with, it is unique in the table (so two concurrent
    deliveries of the same deposit collapse to one row), and it is what the webhook
    matcher finds on a re-delivery or a replay, so nothing is ever created twice.

    ``amount`` is the amount the *event* claims. It is recorded so the deposit is visible
    immediately, but it is deliberately not authoritative: the confirm step replaces it
    with the settled amount the provider's API reports before any receipt is booked.

    Returns ``(intent, created)``.
    """
    entity = virtual_account.entity  # The deposit belongs to the account's ledger entity.
    customer = virtual_account.customer  # May be None; booking then refuses and asks for an operator.
    intent, created = CollectionIntent.objects.get_or_create(
        reference=reference,  # Unique in the table: the DB itself enforces once-only creation.
        defaults=dict(
            entity=entity, provider=virtual_account.provider,
            channel=CollectionChannel.VIRTUAL_ACCOUNT,  # Not a checkout: money arrived by bank transfer.
            provider_reference=provider_reference, amount=amount,
            currency=virtual_account.currency or _entity_currency(entity),
            customer=customer, virtual_account=virtual_account,
            deposit_account=virtual_account.deposit_account,  # Land the receipt in the account's own bank GL.
            payer_email=(customer.billing_email if customer else ""),
            payer_name=(customer.name if customer else virtual_account.account_name),
            # Deliberately no account number in the narration: it is FLS-restricted on
            # the virtual-account serializer, and the narration is copied onto the
            # finance receipt, which has no such protection.
            narration="Virtual account deposit.",
            metadata={"source": "virtual_account_deposit", "webhook_event_type": event_type},
            status=CollectionStatus.PENDING,  # Nothing is settled until the provider is re-verified.
        ),
    )
    if created:  # Audit only the first sighting so a re-delivery adds no second row.
        audit.record(
            action=PaymentAuditAction.COLLECTION_INITIATED, entity=entity,
            provider=virtual_account.provider, reference=reference,
            message=f"Recorded {amount} kobo deposit into a virtual account.",
            metadata={"channel": CollectionChannel.VIRTUAL_ACCOUNT,
                      "virtual_account_id": virtual_account.pk},
        )
    return intent, created  # Hand the intent back for the ordinary confirm path.


# Handle the create virtual account workflow.
def create_virtual_account(*, entity, customer, provider=None, deposit_account=None,
                           bank_code="", actor_user=None):
    """Provision a dedicated virtual NUBAN for ``customer`` and store it."""
    from django.conf import settings

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Resolve the PSP to use.
    
    if VirtualAccount.objects.filter(
        entity=entity, provider=provider_name, customer=customer,
        status=VirtualAccountStatus.ACTIVE,
    ).exists():
        raise ValidationError(
            {"customer": "This customer already has an active virtual account with this provider."})
    
    client = get_provider(provider_name)  # Reuse the configured PSP client.
    reference = _new_reference(entity)  # Give the PSP request its own reference.
    result = client.create_virtual_account(  # Ask the PSP to provision the account.
        reference=reference, customer_name=customer.name,
        customer_email=customer.billing_email, bank_code=bank_code,
    )

    va = VirtualAccount.objects.create(
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
# Handle the set virtual account status workflow.
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
    va.save()

    audit.record(  # Write an audit event so the status flip is visible later.
        action=PaymentAuditAction.VIRTUAL_ACCOUNT_STATUS_CHANGED, entity=va.entity,
        provider=va.provider, reference=va.provider_reference, actor_user=actor_user,
        message=f"Virtual account {va.account_number} set to {status}.",
    )

    return va  # Return the updated virtual account.


@transaction.atomic
# Handle the confirm collection workflow.
def confirm_collection(intent, *, status=None, amount=None, actor_user=None):
    """Confirm a collection and book the receipt - idempotently.

    ``status`` (a :class:`CollectionStatus` value) is taken from a webhook/verify result;
    if omitted, the provider is polled. A SUCCEEDED collection books a customer receipt
    (Dr bank, Cr AR) and links it; FAILED/ABANDONED is recorded with no ledger effect.
    Re-confirming an already-terminal intent is a no-op (returns it unchanged).
    """
    intent = CollectionIntent.objects.select_for_update().get(pk=intent.pk)
    if intent.is_terminal:  # Terminal rows are already settled or failed.
        return intent  # Exit without duplicating ledger work.

    if status is None:  # When no explicit status is supplied, verify with the PSP.
        client = get_provider(intent.provider)  # Resolve the provider using the stored intent value.
        result = client.verify_collection(  # Ask the PSP for the final collection state.
            reference=intent.reference, provider_reference=intent.provider_reference,
        )
        status = result.status  # Trust the PSP status for the confirmation decision.
        # Falling back to intent.amount is safe for a collection we initiated: that amount
        # is the one *we* asked the payer for. It is not safe for a virtual-account
        # deposit, where the intent was built from the inbound event, so the fallback
        # would be the webhook's own claim - exactly the thing this re-verification
        # exists to distrust. With no provider-reported amount there is nothing
        # authoritative to book, so refuse: the event is marked FAILED and surfaces on
        # the operator's needs-attention list with the money still visibly unrecorded.
        if (result.amount <= 0 and intent.virtual_account_id
                and intent.channel == CollectionChannel.VIRTUAL_ACCOUNT):
            raise PaymentStateError(
                "Provider did not report a settled amount for this virtual account "
                "deposit; booking held for manual review.")
        amount = result.amount or intent.amount  # Fall back to the original amount if the PSP omits it.
        intent.raw_response = {**(intent.raw_response or {}), "verify": result.raw}  # Append the verification payload.

    if status != CollectionStatus.SUCCEEDED:  # Only success books a receipt.
        intent.status = (CollectionStatus.FAILED if status == CollectionStatus.FAILED
                         else CollectionStatus.ABANDONED if status == CollectionStatus.ABANDONED
                         else intent.status)
        intent.save(update_fields=["status", "raw_response", "updated_at"])
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
    intent.confirmed_at = timezone.now()
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


# Support the book receipt workflow.
def _book_receipt(intent, *, actor_user=None):
    """Create + post the ``vs_finance.Payment`` for a succeeded collection."""
    from vs_finance.models import Payment
    from vs_finance.receivables import post_payment

    if (intent.virtual_account_id  # If the receipt landed on a virtual account...
            and intent.virtual_account.status == VirtualAccountStatus.INACTIVE):  # ...and that account is inactive...
        raise PaymentStateError(
            "Virtual account is inactive; deposit held for manual review.")
    
    if intent.customer_id is None:  # Receipts need a customer so receivables can be posted correctly.
        raise PaymentStateError(
            "Cannot book a receipt: the collection has no customer (AR sub-ledger).",
        )
    
    deposit = intent.deposit_account or resolve_account(  # Use the configured cash/bank account when none was provided.
        intent.entity, CASH_BANK_CODE, label="Cash & bank",
    )

    received = datetime.date.today()
    payment = Payment.objects.create(
        entity=intent.entity, customer=intent.customer,
        payment_date=received, currency=intent.currency,
        method=PaymentMethod.ONLINE, amount=intent.amount, deposit_account=deposit,
        reference=intent.reference,
        narration=intent.narration or f"Gateway collection {intent.reference}",
    )

    # A receipt cannot settle an invoice that is not raised yet - crediting AR before
    # the invoice debits it drives the control negative for the gap, and the posting
    # service now refuses an explicit allocation that does so. Here that refusal must
    # not be allowed to surface: the payer's money has already moved, and failing the
    # booking would leave real cash unrecorded while the PSP retries a call that can
    # never succeed. A payment against a future-dated invoice is simply a prepayment,
    # so it parks as customer credit and is applied once the invoice exists.
    settles_now = bool(intent.invoice_id) and intent.invoice.invoice_date <= received
    if settles_now:  # Invoice-linked receipts should settle that invoice directly.
        post_payment(payment, actor_user=actor_user,
                     allocations=[(intent.invoice, intent.amount)])  # Allocate the full settled amount to the invoice.
    else:  # Standalone or not-yet-raised invoice: never guess at invoice allocation.
        # Leave the funds as customer credit instead of auto-allocating them.
        post_payment(payment, actor_user=actor_user, auto_allocate=False)  # Park the money as credit instead.

    intent.payment = payment  # Link the payment back to the gateway record.


# --------------------------------------------------------------------------- #
# Payouts (money out)                                                          #
# --------------------------------------------------------------------------- #

# Handle the initiate payout workflow.
def initiate_payout(*, entity, amount, beneficiary_name, beneficiary_account_number,
                    beneficiary_bank_code, vendor=None, source_account=None,
                    provider=None, narration="", currency=None, wht_amount=0,
                    metadata=None, actor_user=None):
    """Create a :class:`PayoutInstruction` and ask the provider to transfer funds out.

    The ledger entry (a vendor payment) is booked only on *confirmation*. If ``vendor``
    is given it is recorded as a loose reference so confirm can re-resolve and book a
    ``VendorPayment``; the WHT split (``wht_amount``) flows through to that posting.
    """
    from django.conf import settings

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Resolve the PSP to use.
    client = get_provider(provider_name)  # Prepare the transfer client up front.
    reference = _new_reference(entity)  # Assign a unique payout reference.
    currency = currency or _entity_currency(entity)  # Default to the entity currency for outbound transfers.

    payout = PayoutInstruction.objects.create(
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


# Support the dispatch transfer workflow.
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
        payout.save(update_fields=["status", "failure_reason", "updated_at"])
        audit.record_rejection(  # Emit a rejection event for the failed payout request.
            action=PaymentAuditAction.PAYOUT_INITIATED, exc=exc, entity=payout.entity,
            provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        )
        raise

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

# Handle the create payout batch workflow.
def create_payout_batch(*, entity, items, provider=None, source_account=None,
                        title="", narration="", currency=None, actor_user=None):
    """Assemble a :class:`PayoutBatch` plus its child ``PENDING`` instructions (no submit).

    ``items`` is an iterable of dicts, each with ``amount`` (kobo) and beneficiary fields
    (``beneficiary_name``, ``beneficiary_account_number``, ``beneficiary_bank_code``) and
    optional ``vendor`` / ``narration`` / ``wht_amount`` / ``metadata`` / ``source_account``.
    Nothing is sent to the provider yet - call :func:`submit_payout_batch` for that.
    """
    from django.conf import settings

    items = list(items)  # Materialize the iterable so it can be counted and iterated safely.
    if not items:  # A batch with no items is not meaningful.
        raise PaymentStateError("A payout batch must contain at least one item.")

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")  # Resolve the batch PSP.
    get_provider(provider_name)  # Validate the provider configuration before creating batch rows.
    currency = currency or _entity_currency(entity)  # Default the batch currency to the entity currency.
    batch_reference = _new_reference(entity)  # Use one reference for the whole batch.

    with transaction.atomic():
        batch = PayoutBatch.objects.create(
            entity=entity, provider=provider_name, reference=batch_reference,
            title=title, narration=narration, currency=currency,
            source_account=source_account, status=PayoutBatchStatus.DRAFT,
            created_by=actor_user,
        )
        total = 0  # Accumulate the batch total as each instruction is added.
        for item in items:  # Each dict becomes one payout instruction.
            amount = int(item.get("amount") or 0)
            if amount <= 0:  # Reject empty or negative payout lines.
                raise PaymentStateError("Each payout item needs a positive amount (kobo).")
            vendor = item.get("vendor")
            PayoutInstruction.objects.create(
                entity=entity, batch=batch, provider=provider_name,
                reference=_new_reference(entity), amount=amount, currency=currency,
                beneficiary_name=item["beneficiary_name"],
                beneficiary_account_number=item["beneficiary_account_number"],
                beneficiary_bank_code=item.get("beneficiary_bank_code", ""),
                source_account=item.get("source_account") or source_account,
                narration=item.get("narration", "") or narration,
                status=PayoutStatus.PENDING,
                vendor_source_type="vs_procurement.Vendor" if vendor else "",
                vendor_source_id=str(vendor.pk) if vendor else "",
                metadata={**(item.get("metadata") or {}),
                          "wht_amount": int(item.get("wht_amount") or 0)},
                created_by=actor_user,
            )
            total += amount  # Keep the running batch total in sync.
        batch.total_amount = total  # Store the aggregate amount on the batch.
        batch.item_count = len(items)  # Store the number of instructions on the batch.
        batch.save(update_fields=["total_amount", "item_count", "updated_at"])

    audit.record(  # Write a batch-level audit event after the transaction commits.
        action=PaymentAuditAction.PAYOUT_BATCH_CREATED, entity=entity,
        provider=provider_name, reference=batch_reference, actor_user=actor_user,
        message=f"Created payout batch of {len(items)} items, {total} kobo.",
    )
    return batch  # Return the draft batch for later submission.


# Handle the submit payout batch workflow.
def submit_payout_batch(batch, *, actor_user=None):
    """Submit every ``PENDING`` instruction in ``batch`` to the provider, one by one.

    Each item rides the shared :func:`_dispatch_transfer`; a per-item provider rejection
    marks that instruction FAILED but does not abort the run. The batch's aggregate status
    is recomputed from its children afterwards. Idempotent: already-dispatched items
    (non-PENDING) are skipped, so re-submitting a partially-failed batch only retries the
    stragglers.
    """
    submitted = failed = 0  # Track how many instructions were accepted or rejected.
    for payout in batch.instructions.filter(status=PayoutStatus.PENDING):
        try:  # One failed instruction should not abort the whole batch.
            _dispatch_transfer(payout, actor_user=actor_user)  # Submit this payout to the provider.
            submitted += 1  # Count successful submissions.
        except FinanceError:  # Keep going so later rows still have a chance to submit.
            failed += 1  # Count provider rejections.

    batch.submitted_at = batch.submitted_at or timezone.now()
    _recompute_batch_status(batch)  # Recalculate the batch status from its children.
    audit.record(  # Emit the batch submission audit event with the outcome counts.
        action=PaymentAuditAction.PAYOUT_BATCH_SUBMITTED, entity=batch.entity,
        provider=batch.provider, reference=batch.reference, actor_user=actor_user,
        message=f"Submitted batch: {submitted} dispatched, {failed} failed.",
        metadata={"submitted": submitted, "failed": failed},
    )
    return batch  # Return the batch after aggregate status refresh.


# Support the recompute batch status workflow.
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
        batch.save(update_fields=["status", "submitted_at", "updated_at"])
    return batch  # Return the refreshed batch.


@transaction.atomic
# Handle the confirm payout workflow.
def confirm_payout(payout, *, status=None, amount=None, actor_user=None):
    """Confirm a payout and book the vendor payment - idempotently.

    ``amount`` (kobo) optionally overrides the booked amount; if omitted on the verify
    path, the provider-reported settled amount is adopted (mirrors ``confirm_collection``).
    """
    payout = PayoutInstruction.objects.select_for_update().get(pk=payout.pk)
    if payout.is_terminal:  # Already confirmed or failed rows should not be processed again.
        return payout  # Exit early for idempotency.

    if status is None:  # Ask the PSP when the caller did not provide a terminal status.
        client = get_provider(payout.provider)  # Resolve the correct provider adapter.
        result = client.verify_transfer(  # Fetch the current transfer state from the PSP.
            reference=payout.reference, provider_reference=payout.provider_reference,
        )
        status = result.status  # Use the provider's transfer status for confirmation.
        amount = result.amount or payout.amount  # Adopt the PSP's settled amount, falling back to the instructed value.
        payout.raw_response = {**(payout.raw_response or {}), "verify": result.raw}  # Append the verification payload.

    if status != PayoutStatus.PAID:  # Only a paid transfer can book a vendor payment.
        if status in (PayoutStatus.FAILED, PayoutStatus.REVERSED):  # Preserve only terminal negative outcomes locally.
            payout.status = status  # Mirror the final failure state.
        payout.save(update_fields=["status", "raw_response", "updated_at"])
        audit.record(  # Record the failed payout confirmation for auditability.
            action=PaymentAuditAction.PAYOUT_FAILED, entity=payout.entity,
            provider=payout.provider, reference=payout.reference, succeeded=False,
            message=f"Payout ended '{status}'.", actor_user=actor_user,
        )
        _refresh_batch(payout)  # Keep the parent batch aggregate in sync.
        return payout  # Stop because no vendor payment should be posted.

    settled = amount or payout.amount  # Use the confirmed amount when one is available.
    if settled > 0 and settled != payout.amount:  # Preserve the originally instructed amount in metadata.
        # Book the amount that actually left the account, but retain the instructed value for audit.
        payout.metadata = {**(payout.metadata or {}), "instructed_amount": payout.amount}  # Store the pre-settlement amount.
        payout.amount = settled  # Replace the payout amount with the actual settled amount.

    _book_vendor_payment(payout, actor_user=actor_user)  # Post the vendor payment into the ledger.
    payout.status = PayoutStatus.PAID  # Mark the payout as successfully settled.
    payout.confirmed_at = timezone.now()
    payout.save(update_fields=[
        "status", "vendor_payment_id", "amount", "metadata", "confirmed_at", "raw_response", "updated_at",
    ])
    audit.record(  # Emit the successful confirmation audit event.
        action=PaymentAuditAction.PAYOUT_CONFIRMED, entity=payout.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        message=f"Booked vendor payment for {payout.amount} kobo.",
        metadata={"vendor_payment_id": payout.vendor_payment_id},
    )
    _refresh_batch(payout)  # Refresh the parent batch after the child status changes.
    return payout  # Return the confirmed payout instruction.


# Support the refresh batch workflow.
def _refresh_batch(payout):
    """Recompute the owning batch's aggregate status after a child changed, if any."""
    if payout.batch_id:  # Only child payouts inside a batch need refresh work.
        _recompute_batch_status(
            PayoutBatch.objects.select_for_update().get(pk=payout.batch_id)
        )


# Support the book vendor payment workflow.
def _book_vendor_payment(payout, *, actor_user=None):
    """Create + post the ``vs_procurement.VendorPayment`` for a paid payout."""
    if not payout.vendor_source_id:  # Vendor-backed payouts need a source reference to post AP correctly.
        raise PaymentStateError(
            "Cannot book a vendor payment: the payout has no vendor reference.",
        )
    from vs_procurement.models import Vendor, VendorPayment
    from vs_procurement.payables import post_vendor_payment
    from vs_procurement.constants import ProcApprovalState

    vendor = Vendor.objects.get(pk=int(payout.vendor_source_id))
    wht = int((payout.metadata or {}).get("wht_amount", 0))
    vp = VendorPayment.objects.create(
        entity=payout.entity, vendor=vendor, payment_date=datetime.date.today(),
        currency=payout.currency, method=PaymentMethod.BANK_TRANSFER,
        gross_amount=payout.amount, wht_amount=wht,
        net_amount=payout.amount - wht,
        payment_account=payout.source_account or resolve_account(
            payout.entity, CASH_BANK_CODE, label="Cash & bank",
        ),
        reference=payout.reference,
        narration=payout.narration or f"Gateway payout {payout.reference}",
        # System-approved: this vendor payment records a disbursement the gateway
        # has already made against an authorised payout instruction, so it does
        # not re-enter the interactive vendor-payment approval workflow.
        approval_state=ProcApprovalState.APPROVED,
    )
    # System-originated: the gateway has already disbursed against an authorised
    # payout, so posting skips the pre-disbursement governance/eligibility gates.
    post_vendor_payment(vp, actor_user=actor_user, system_originated=True)  # Post the AP movement.
    payout.vendor_payment_id = vp.pk  # Link the payout instruction to the vendor payment.
