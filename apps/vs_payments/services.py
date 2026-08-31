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
import hashlib
import json
import logging
from typing import NamedTuple

from django.db import IntegrityError, transaction
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
    WF_DEFAULT_HIGH_VALUE_THRESHOLD,
    VirtualAccountStatus,
)
from .exceptions import (
    IdempotencyConflictError,
    PaymentStateError,
    PayoutApprovalRequiredError,
    ProviderDispatchInTransactionError,
)
from .models import CollectionIntent, PayoutBatch, PayoutInstruction, VirtualAccount
from .providers.registry import get_provider

_logger = logging.getLogger("vs_payments.services")  # Diagnostics for off-request dispatch work.


# --------------------------------------------------------------------------- #
# Irreversible-side-effect guards                                             #
# --------------------------------------------------------------------------- #

#: Provider failures that say nothing about whether the money moved. A timeout, a
#: dropped connection or a 5xx means the request may well have been accepted and only
#: the answer was lost; 429/408/425 are the same story with a name on it. Only a clean
#: 4xx rejection is proof that nothing was sent.
_INDETERMINATE_PROVIDER_STATUSES = frozenset({408, 425, 429})


# Support the outbound transaction guard.
def _assert_no_open_transaction(what: str) -> None:
    """Refuse an irreversible provider call while a database transaction is open.

    A transaction can roll back; a bank transfer cannot. If the provider is called
    inside ``transaction.atomic`` and the enclosing block later rolls back, the money
    has moved while every local trace of it disappears: the instruction reverts to
    PENDING, the batch reverts to DRAFT, and the next run sends the same money again.
    The guard sits at the choke point every dispatch passes through, so the mistake
    cannot be reintroduced by a new caller further up.

    Django's own ``atomic(durable=True)`` check is the model here: the atomic block
    ``django.test.TestCase`` opens to wrap a test carries ``_from_testcase`` and is not
    an application transaction, so it does not trip the guard.
    """
    blocks = transaction.get_connection().atomic_blocks
    if blocks and not getattr(blocks[-1], "_from_testcase", False):
        raise ProviderDispatchInTransactionError(
            f"{what} must run outside a database transaction: a rollback would erase "
            "the local record of money that has already left the account.",
        )


# Support the provider failure classification workflow.
def _provider_failure_is_final(exc) -> bool:
    """True only when the provider definitely rejected the request without moving money.

    Marking an indeterminate failure FAILED is how transferred money gets recorded as
    a failure, so anything short of a clean rejection leaves the instruction PROCESSING
    for verification (webhook or ``confirm_payout``) to settle.
    """
    code = getattr(exc, "provider_code", None)
    if not code or not str(code).isdigit():  # Transport failure or unparseable body.
        return False
    status = int(code)
    if status in _INDETERMINATE_PROVIDER_STATUSES or status >= 500:
        return False
    return 400 <= status < 500


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
        # A gateway receipt continues the customer's chain exactly as a
        # counter receipt does. Without this the online path - which is how most
        # parents actually pay - would leave every receipt school-wide while the
        # invoice it settles sat in a branch, splitting one family's ledger
        # across two scopes.
        branch=intent.customer.branch,
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
    """Refuse the retired standalone cash-out route.

    Callers must create a one-item :class:`PayoutBatch` and submit that batch for
    workflow approval. Keeping this function as an explicit refusal prevents an old
    worker or integration from silently retaining the former direct-dispatch behavior.
    """
    raise PayoutApprovalRequiredError(
        "Standalone payout dispatch is disabled. Create a payout batch and submit it "
        "for approval.",
    )


def _normalize_account_name(value) -> str:
    """Canonical account name used only for compatibility comparisons."""
    return " ".join(str(value or "").split()).casefold()


def _normalize_account_number(value) -> str:
    """Canonical provider account number without presentation whitespace."""
    return "".join(str(value or "").split()).upper()


def _normalize_bank_code(value) -> str:
    """Canonical provider bank code."""
    return str(value or "").strip().upper()


def _eligible_vendor_snapshot(entity, vendor, item) -> dict:
    """Validate one vendor master and return its provider-bound destination."""
    from vs_procurement.constants import VendorKycStatus

    if vendor is None or vendor.entity_id != entity.pk:
        raise ValidationError({"vendor": "No such vendor in this entity."})
    if not vendor.is_active:
        raise ValidationError({"vendor": "Select an active vendor for payout."})
    if vendor.kyc_status != VendorKycStatus.VERIFIED:
        raise ValidationError({"vendor": "The vendor must have verified KYC before payout."})
    if vendor.on_hold:
        raise ValidationError({"vendor": "This vendor is on hold and cannot be paid."})

    snapshot = {
        "beneficiary_name": str(vendor.bank_account_name or "").strip(),
        "beneficiary_account_number": _normalize_account_number(vendor.bank_account_number),
        "beneficiary_bank_code": _normalize_bank_code(vendor.bank_code),
    }
    missing = [
        field for field, value in (
            ("bank_account_name", snapshot["beneficiary_name"]),
            ("bank_account_number", snapshot["beneficiary_account_number"]),
            ("bank_code", snapshot["beneficiary_bank_code"]),
        ) if not value
    ]
    if missing:
        raise ValidationError({
            "vendor": "The vendor needs a verified account name, account number, and bank code.",
            "missing_bank_fields": missing,
        })

    supplied = (
        ("beneficiary_name", _normalize_account_name, snapshot["beneficiary_name"]),
        (
            "beneficiary_account_number", _normalize_account_number,
            snapshot["beneficiary_account_number"],
        ),
        ("beneficiary_bank_code", _normalize_bank_code, snapshot["beneficiary_bank_code"]),
    )
    for field, normalize, master_value in supplied:
        if field in item and item[field] not in (None, ""):
            if normalize(item[field]) != normalize(master_value):
                raise ValidationError({
                    field: "The supplied beneficiary value does not match the verified vendor master.",
                })
    return snapshot


def _canonical_batch_payload(
    *, items, provider, source_account, title, narration, request_kind,
    submit_for_approval,
) -> dict:
    """Build the stable, normalized payload bound to an idempotency key."""
    normalized_items = []
    for item in items:
        vendor = item.get("vendor")
        normalized_items.append({
            "amount": int(item.get("amount") or 0),
            "vendor_id": getattr(vendor, "pk", None),
            "source_account_id": getattr(item.get("source_account") or source_account, "pk", None),
            "narration": str(item.get("narration", "") or narration).strip(),
            "wht_amount": int(item.get("wht_amount") or 0),
            "metadata": item.get("metadata") or {},
            "beneficiary_name": (
                _normalize_account_name(item.get("beneficiary_name"))
                if "beneficiary_name" in item else None
            ),
            "beneficiary_account_number": (
                _normalize_account_number(item.get("beneficiary_account_number"))
                if "beneficiary_account_number" in item else None
            ),
            "beneficiary_bank_code": (
                _normalize_bank_code(item.get("beneficiary_bank_code"))
                if "beneficiary_bank_code" in item else None
            ),
        })
    return {
        "request_kind": request_kind,
        "submit_for_approval": bool(submit_for_approval),
        "provider": str(provider).upper(),
        "source_account_id": getattr(source_account, "pk", None),
        "title": str(title or "").strip(),
        "narration": str(narration or "").strip(),
        "items": normalized_items,
    }


def payout_request_fingerprint(**kwargs) -> str:
    """SHA-256 of the canonical payout creation request."""
    payload = _canonical_batch_payload(**kwargs)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_instruction_snapshot(payout, vendor) -> None:
    """Recheck current eligibility and the exact destination approved earlier."""
    snapshot = _eligible_vendor_snapshot(payout.entity, vendor, {})
    if (
        _normalize_account_name(payout.beneficiary_name)
        != _normalize_account_name(snapshot["beneficiary_name"])
        or _normalize_account_number(payout.beneficiary_account_number)
        != snapshot["beneficiary_account_number"]
        or _normalize_bank_code(payout.beneficiary_bank_code)
        != snapshot["beneficiary_bank_code"]
    ):
        raise PaymentStateError(
            "The vendor bank destination changed after this payout was created. "
            "Create and approve a new payout batch.",
        )


def _validate_approved_instance(batch, approved_instance):
    """Require exact terminal approval and the minimum distinct human votes."""
    from django.contrib.contenttypes.models import ContentType
    from vs_workflow.constants import (
        WorkflowInstanceStatus,
        WorkflowStageAction as StageActionEnum,
    )
    from vs_workflow.models import WorkflowInstance, WorkflowStageAction

    if approved_instance is None:
        raise PayoutApprovalRequiredError()
    instance = WorkflowInstance.all_objects.filter(pk=approved_instance.pk).first()
    expected_ct = ContentType.objects.get_for_model(PayoutBatch)
    if (
        instance is None
        or instance.status != WorkflowInstanceStatus.APPROVED
        or instance.document_type != batch.workflow_document_type
        or instance.document_content_type_id != expected_ct.pk
        or instance.document_object_id != str(batch.pk)
        or instance.tenant_id != batch.entity.tenant_id
    ):
        raise PayoutApprovalRequiredError(
            "Provider dispatch requires the terminal approved workflow instance for "
            "this exact payout batch.",
        )

    actor_ids = set(
        WorkflowStageAction.objects.filter(
            stage_instance__instance=instance,
            action=StageActionEnum.APPROVED,
            reversed_at__isnull=True,
            is_reversal_of__isnull=True,
        ).exclude(actor_id=instance.requested_by_id).values_list("actor_id", flat=True)
    )
    required = 2 if batch.total_amount >= WF_DEFAULT_HIGH_VALUE_THRESHOLD else 1
    if len(actor_ids) < required:
        requirement = (
            "two distinct human approvers"
            if required == 2 else "a human approver distinct from the requester"
        )
        raise PayoutApprovalRequiredError(
            f"Provider dispatch requires {requirement} for this payout batch.",
            required_approvers=required,
            distinct_approved_actors=len(actor_ids),
        )
    return instance


# Support the dispatch transfer workflow.
def _dispatch_transfer(
    payout, *, batch=None, approved_instance=None, vendor=None,
    client=None, metadata=None, actor_user=None,
):
    """Ask the provider to transfer funds for an already-created ``PENDING`` payout.

    This private boundary still validates the exact approved batch so a direct caller
    cannot turn it back into a standalone cash-out route.

    The instruction is **claimed** - moved PENDING -> PROCESSING in its own committed
    transaction - *before* the provider is called, and the call itself is made with no
    transaction open. That ordering is what makes the send safe to crash through: a
    process that dies mid-flight leaves a PROCESSING row that no retry will send again,
    and that the webhook or :func:`confirm_payout` settles from the provider's own
    record. Only a clean provider rejection turns the row FAILED; an indeterminate
    failure (timeout, 5xx, unreachable host) leaves it PROCESSING, because the money
    may well have moved.
    """
    from vs_procurement.models import Vendor

    _assert_no_open_transaction("Dispatching a payout instruction to the provider")
    if payout.batch_id is None or batch is None or payout.batch_id != batch.pk:
        raise PayoutApprovalRequiredError(
            "A payout instruction can be dispatched only through its approved batch.",
        )
    _validate_approved_instance(batch, approved_instance)
    if vendor is None:
        if not payout.vendor_source_id:
            raise PaymentStateError("The payout instruction has no vendor master reference.")
        vendor = Vendor.objects.filter(pk=int(payout.vendor_source_id)).first()
    _validate_instruction_snapshot(payout, vendor)
    client = client or get_provider(payout.provider)  # Allow callers to reuse or lazily resolve the PSP client.

    # Claim the row and re-check the destination in one committed transaction. The
    # re-read is not belt-and-braces: the caller validated against a vendor row it read
    # before the locks were released, and the destination that matters is the one that
    # is true at the moment of the send.
    with transaction.atomic():
        payout = PayoutInstruction.objects.select_for_update().get(pk=payout.pk)
        if payout.status != PayoutStatus.PENDING:
            # Another dispatch already claimed it. Never send twice.
            return payout
        _validate_instruction_snapshot(
            payout, Vendor.objects.select_for_update().get(pk=vendor.pk),
        )
        payout.status = PayoutStatus.PROCESSING  # In flight from here on, whatever happens.
        payout.save(update_fields=["status", "updated_at"])

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
        reason = str(getattr(exc, "message", exc))[:255]  # Store a short human-readable failure reason.
        payout.failure_reason = reason
        fields = ["failure_reason", "updated_at"]
        if _provider_failure_is_final(exc):  # A clean rejection: nothing was sent.
            payout.status = PayoutStatus.FAILED
            fields.append("status")
        else:
            # The provider may have accepted this transfer and failed to tell us. Leaving
            # it PROCESSING keeps it out of every retry path and on the reconciliation
            # report until the provider's own record settles it either way.
            payout.metadata = {
                **(payout.metadata or {}), "dispatch_outcome_unknown": True,
            }
            fields.append("metadata")
        payout.save(update_fields=fields)
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
def create_payout_batch(
    *, entity, items, provider=None, source_account=None,
    title="", narration="", currency=None, actor_user=None,
    idempotency_key="", request_kind="batch", submit_for_approval=False,
):
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

    provider_name = str(
        provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")
    ).strip().upper()  # Resolve the batch PSP.
    currency = currency or _entity_currency(entity)  # Default the batch currency to the entity currency.
    fingerprint = payout_request_fingerprint(
        items=items, provider=provider_name, source_account=source_account,
        title=title, narration=narration, request_kind=request_kind,
        submit_for_approval=submit_for_approval,
    )
    idempotency_key = str(idempotency_key or "")

    with transaction.atomic():
        if idempotency_key:
            existing = PayoutBatch.objects.filter(
                entity=entity, idempotency_key=idempotency_key,
            ).first()
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError()
                existing._idempotency_replay = True
                return existing

        snapshots = []
        for item in items:
            amount = int(item.get("amount") or 0)
            if amount <= 0:
                raise ValidationError({"amount": "Each payout item needs a positive amount (kobo)."})
            wht_amount = int(item.get("wht_amount") or 0)
            if wht_amount < 0 or wht_amount > amount:
                raise ValidationError({
                    "wht_amount": "Withholding tax must be between zero and the payout amount.",
                })
            snapshots.append(_eligible_vendor_snapshot(entity, item.get("vendor"), item))

        get_provider(provider_name)  # Validate configuration without sending money.
        batch_reference = _new_reference(entity)  # Use one reference for the whole batch.
        try:
            # Keep an idempotency-key race inside a savepoint so the winning row can
            # be read without leaving the surrounding creation transaction broken.
            with transaction.atomic():
                batch = PayoutBatch.objects.create(
                    entity=entity, provider=provider_name, reference=batch_reference,
                    idempotency_key=idempotency_key, request_fingerprint=fingerprint,
                    title=str(title or "").strip(), narration=str(narration or "").strip(),
                    currency=currency, source_account=source_account,
                    status=PayoutBatchStatus.DRAFT, created_by=actor_user,
                )
        except IntegrityError:
            existing = PayoutBatch.objects.filter(
                entity=entity, idempotency_key=idempotency_key,
            ).first() if idempotency_key else None
            if existing is None:
                raise
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflictError()
            existing._idempotency_replay = True
            return existing

        total = 0  # Accumulate the batch total as each instruction is added.
        for item, snapshot in zip(items, snapshots):  # Each dict becomes one payout instruction.
            amount = int(item.get("amount") or 0)
            vendor = item.get("vendor")
            PayoutInstruction.objects.create(
                entity=entity, batch=batch, provider=provider_name,
                reference=_new_reference(entity), amount=amount, currency=currency,
                beneficiary_name=snapshot["beneficiary_name"],
                beneficiary_account_number=snapshot["beneficiary_account_number"],
                beneficiary_bank_code=snapshot["beneficiary_bank_code"],
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
        batch._idempotency_replay = False

    audit.record(  # Write a batch-level audit event after the transaction commits.
        action=PaymentAuditAction.PAYOUT_BATCH_CREATED, entity=entity,
        provider=provider_name, reference=batch_reference, actor_user=actor_user,
        message=f"Created payout batch of {len(items)} items, {total} kobo.",
    )
    return batch  # Return the draft batch for later submission.


@transaction.atomic
def submit_payout_batch_for_approval(batch, *, requested_by):
    """Submit one batch once, returning the existing instance on a replay."""
    from vs_workflow.models import WorkflowInstance
    from vs_workflow.services.submission import submit_for_approval

    locked = PayoutBatch.objects.select_for_update().get(pk=batch.pk)
    existing = (
        WorkflowInstance.all_objects.for_document(locked)
        .order_by("-created_at", "-id")
        .first()
    )
    if existing is not None:
        return existing
    return submit_for_approval(locked, requested_by=requested_by)


class _PreparedDispatch(NamedTuple):
    """The locked, validated state a batch dispatch runs against."""

    batch: PayoutBatch  # Re-read under ``select_for_update``.
    approved_instance: object  # The terminal workflow instance that authorised the batch.
    pending: list  # Instructions still to send, in pk order.
    vendor_ids: list  # Vendor master pk per pending instruction, positionally aligned.
    vendors: dict  # Vendor pk -> locked vendor row.
    client: object  # Configured PSP adapter; resolving it sends nothing.


# Support the pre-dispatch validation workflow.
def _prepare_batch_dispatch(batch, approved_instance) -> _PreparedDispatch:
    """Re-check everything that must hold before a single naira leaves the account.

    Deliberately safe to call *inside* a transaction: it performs no provider I/O
    (:func:`get_provider` only reads configuration). That is what lets the approval
    handler run these checks in the approval transaction, so a batch that could never
    be sent - stale vendor bank details, drifted totals, missing human votes - fails the
    approval instead of being approved into a dispatch that can only ever fail.
    """
    from vs_procurement.models import Vendor

    batch = PayoutBatch.objects.select_for_update().get(pk=batch.pk)
    approved_instance = _validate_approved_instance(batch, approved_instance)
    instructions = list(batch.instructions.select_for_update().order_by("pk"))
    if batch.item_count != len(instructions) or batch.total_amount != sum(
        payout.amount for payout in instructions
    ):
        raise PaymentStateError(
            "The payout batch totals no longer match its locked instructions. "
            "Review and recreate the batch.",
        )
    if any(
        payout.entity_id != batch.entity_id or payout.provider != batch.provider
        for payout in instructions
    ):
        raise PaymentStateError("A payout instruction does not match its owning batch.")

    pending = [payout for payout in instructions if payout.status == PayoutStatus.PENDING]
    vendor_ids = []
    for payout in pending:
        if payout.vendor_source_type != "vs_procurement.Vendor" or not payout.vendor_source_id:
            raise PaymentStateError("Every payout instruction must reference a vendor master.")
        try:
            vendor_ids.append(int(payout.vendor_source_id))
        except (TypeError, ValueError) as exc:
            raise PaymentStateError("A payout instruction has an invalid vendor reference.") from exc
    vendors = {
        vendor.pk: vendor
        for vendor in Vendor.objects.select_for_update().filter(pk__in=sorted(set(vendor_ids)))
    }
    for payout, vendor_id in zip(pending, vendor_ids):
        vendor = vendors.get(vendor_id)
        if vendor is None:
            raise PaymentStateError("A payout instruction's vendor master no longer exists.")
        _validate_instruction_snapshot(payout, vendor)

    return _PreparedDispatch(
        batch=batch, approved_instance=approved_instance, pending=pending,
        vendor_ids=vendor_ids, vendors=vendors,
        client=get_provider(batch.provider),  # Configuration check, not a send.
    )


# Handle the pre-dispatch validation workflow.
def validate_payout_batch_dispatch(batch, *, approved_instance) -> None:
    """Raise if this batch could not be dispatched right now. Sends nothing.

    The approval handler calls this inside the approval transaction so an unsendable
    batch is refused at the point a human clicks Approve, where the message can still
    reach them, rather than after the approval has committed.
    """
    _prepare_batch_dispatch(batch, approved_instance)


# Handle the submit payout batch workflow.
def submit_payout_batch(batch, *, approved_instance=None, actor_user=None):
    """Submit every ``PENDING`` instruction in ``batch`` to the provider, one by one.

    Deliberately **not** ``@transaction.atomic``. The run has three phases, and the
    boundary between the first two is the whole point:

    1. validate and lock in one transaction - approval, totals, ownership, vendor
       destinations, provider configuration - and **commit** it;
    2. send, with no transaction open, one instruction at a time. Each item claims
       itself before its own send (see :func:`_dispatch_transfer`), which is what makes
       double-sending impossible without holding locks across a network call;
    3. recompute the batch aggregate in a fresh transaction.

    A per-item provider rejection marks that instruction FAILED but does not abort the
    run. Idempotent: already-dispatched items (non-PENDING) are skipped, so re-running a
    partially-failed batch only retries the stragglers.
    """
    _assert_no_open_transaction("Submitting a payout batch to the provider")

    with transaction.atomic():  # Phase 1: everything that must be true before any money moves.
        prepared = _prepare_batch_dispatch(batch, approved_instance)
        batch = prepared.batch
        if not prepared.pending:
            _recompute_batch_status(batch)
            return batch

    # Phase 2: locks released, transaction committed. Money moves only from here.
    approved_instance = prepared.approved_instance
    pending, vendor_ids, vendors, client = (
        prepared.pending, prepared.vendor_ids, prepared.vendors, prepared.client,
    )
    submitted = failed = 0  # Track how many instructions were accepted or rejected.
    for payout, vendor_id in zip(pending, vendor_ids):
        try:  # One failed instruction should not abort the whole batch.
            _dispatch_transfer(
                payout, batch=batch, approved_instance=approved_instance,
                vendor=vendors[vendor_id], client=client,
                metadata=payout.metadata or {}, actor_user=actor_user,
            )  # Submit this payout to the provider.
            submitted += 1  # Count successful submissions.
        except FinanceError:  # Keep going so later rows still have a chance to submit.
            failed += 1  # Count provider rejections.

    with transaction.atomic():  # Phase 3: the aggregate, derived from what the children now say.
        batch = PayoutBatch.objects.select_for_update().get(pk=batch.pk)
        batch.submitted_at = batch.submitted_at or timezone.now()
        _recompute_batch_status(batch)  # Recalculate the batch status from its children.
    audit.record(  # Emit the batch submission audit event with the outcome counts.
        action=PaymentAuditAction.PAYOUT_BATCH_SUBMITTED, entity=batch.entity,
        provider=batch.provider, reference=batch.reference, actor_user=actor_user,
        message=f"Submitted batch: {submitted} dispatched, {failed} failed.",
        metadata={"submitted": submitted, "failed": failed},
    )
    return batch  # Return the batch after aggregate status refresh.


# Handle the post-approval dispatch workflow.
def dispatch_approved_payout_batch(*, batch_id, instance_id, actor_user_id=None):
    """Dispatch one approved batch, off the back of the approval that authorised it.

    This is what the workflow handler schedules for after its transaction commits, and
    what the sweep re-runs for anything the schedule lost. It re-reads everything from
    the database rather than trusting arguments carried across a queue, and it is safe
    to run twice: the approval is re-validated and each instruction claims itself.
    """
    from django.contrib.auth import get_user_model
    from vs_workflow.models import WorkflowInstance

    batch = PayoutBatch.objects.filter(pk=batch_id).first()
    if batch is None:  # Deleted between approval and dispatch; nothing to send.
        return None
    instance = WorkflowInstance.all_objects.filter(pk=instance_id).first()
    actor = (
        get_user_model().objects.filter(pk=actor_user_id).first()
        if actor_user_id else None
    )
    return submit_payout_batch(batch, approved_instance=instance, actor_user=actor)


# Handle the undispatched payout batch sweep workflow.
def sweep_undispatched_payout_batches():
    """Dispatch approved batches whose dispatch never ran.

    Moving the provider call out of the approval transaction buys correctness at the
    price of a gap: the approval commits, and then something has to happen. A broker
    outage, a worker restart between commit and consume, or a crash partway through the
    loop all leave an approved batch sitting on undispatched instructions with nothing
    driving it. This is what closes that gap, and it is why the handler is allowed to
    treat a failed enqueue as a logged warning rather than a lost payout.
    """
    from vs_workflow.constants import WorkflowInstanceStatus
    from vs_workflow.models import WorkflowInstance

    stale = (
        PayoutBatch.objects
        .filter(status=PayoutBatchStatus.DRAFT,
                metadata__approval_status="APPROVED",
                instructions__status=PayoutStatus.PENDING)
        .distinct()
        .order_by("pk")
    )
    dispatched = skipped = failures = 0
    for batch in stale:
        instance = (
            WorkflowInstance.all_objects.for_document(batch)
            .filter(status=WorkflowInstanceStatus.APPROVED)
            .order_by("-created_at", "-id")
            .first()
        )
        if instance is None:  # Metadata says approved but the instance does not; refuse to send.
            skipped += 1
            continue
        try:
            dispatch_approved_payout_batch(batch_id=batch.pk, instance_id=instance.pk)
            dispatched += 1
        except Exception:  # One unsendable batch must not stop the rest of the sweep.
            _logger.exception(
                "sweep_undispatched_payout_batches: batch %s could not be dispatched.",
                batch.pk,
            )
            failures += 1
    return {"dispatched": dispatched, "skipped": skipped, "failures": failures}


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
