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
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_finance.accounts import resolve_account
from vs_finance.constants import CASH_BANK_CODE, PaymentMethod
from vs_finance.exceptions import FinanceError

from . import audit
from .constants import (
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


def _new_reference() -> str:
    """A unique merchant reference / idempotency key for an outbound request."""
    return f"{REFERENCE_PREFIX}-{uuid.uuid4().hex[:20].upper()}"


def _entity_currency(entity):
    return getattr(entity, "base_currency", None)


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
    from .constants import CollectionChannel

    from django.conf import settings

    channel = channel or CollectionChannel.CHECKOUT
    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")
    client = get_provider(provider_name)
    reference = _new_reference()
    callback_url = callback_url or getattr(settings, "PAYMENTS_CALLBACK_URL", "")
    currency = currency or _entity_currency(entity)
    if invoice is not None and customer is None:
        customer = invoice.customer

    intent = CollectionIntent.objects.create(
        entity=entity, provider=provider_name, channel=channel, reference=reference,
        amount=amount, currency=currency, customer=customer, invoice=invoice,
        deposit_account=deposit_account, payer_email=payer_email or
        (customer.billing_email if customer else ""),
        payer_name=payer_name or (customer.name if customer else ""),
        narration=narration, metadata=metadata or {}, created_by=actor_user,
        status=CollectionStatus.PENDING,
    )

    try:
        result = client.create_checkout(
            reference=reference, amount=amount,
            currency=getattr(currency, "code", currency) or "NGN",
            customer_email=intent.payer_email, customer_name=intent.payer_name,
            narration=narration, callback_url=callback_url, metadata=metadata or {},
        )
    except FinanceError as exc:
        intent.status = CollectionStatus.FAILED
        intent.raw_response = {"error": str(getattr(exc, "message", exc))}
        intent.save(update_fields=["status", "raw_response", "updated_at"])
        audit.record_rejection(
            action=PaymentAuditAction.COLLECTION_INITIATED, exc=exc, entity=entity,
            provider=provider_name, reference=reference, actor_user=actor_user,
        )
        raise

    intent.provider_reference = result.provider_reference
    intent.checkout_url = result.checkout_url
    intent.authorization_code = result.authorization_code
    intent.status = CollectionStatus.PROCESSING
    intent.raw_response = result.raw
    intent.save(update_fields=[
        "provider_reference", "checkout_url", "authorization_code", "status",
        "raw_response", "updated_at",
    ])
    audit.record(
        action=PaymentAuditAction.COLLECTION_INITIATED, entity=entity,
        provider=provider_name, reference=reference, actor_user=actor_user,
        message=f"Initiated {amount} kobo collection via {provider_name}.",
        metadata={"channel": channel},
    )
    return intent


def create_virtual_account(*, entity, customer, provider=None, deposit_account=None,
                           bank_code="", actor_user=None):
    """Provision a dedicated virtual NUBAN for ``customer`` and store it."""
    from django.conf import settings

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")
    client = get_provider(provider_name)
    reference = _new_reference()
    result = client.create_virtual_account(
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
    audit.record(
        action=PaymentAuditAction.VIRTUAL_ACCOUNT_CREATED, entity=entity,
        provider=provider_name, reference=reference, actor_user=actor_user,
        message=f"Virtual account {result.account_number} for {customer.code}.",
    )
    return va


@transaction.atomic
def set_virtual_account_status(va, *, status, actor_user=None):
    """Activate or deactivate a virtual account on our side.

    We flip the local status and record it. Provider-side teardown is **not**
    wired (no provider method backs it), so a deactivated account stops being
    offered for new transfers here while remaining whatever it is at the PSP.
    """
    if status not in VirtualAccountStatus.values:
        raise ValidationError({"status": f"Must be one of {', '.join(VirtualAccountStatus.values)}."})
    if va.status == status:
        return va
    va.status = status
    va.save()
    audit.record(
        action=PaymentAuditAction.VIRTUAL_ACCOUNT_STATUS_CHANGED, entity=va.entity,
        provider=va.provider, reference=va.provider_reference, actor_user=actor_user,
        message=f"Virtual account {va.account_number} set to {status}.",
    )
    return va


@transaction.atomic
def confirm_collection(intent, *, status=None, amount=None, actor_user=None):
    """Confirm a collection and book the receipt — idempotently.

    ``status`` (a :class:`CollectionStatus` value) is taken from a webhook/verify result;
    if omitted, the provider is polled. A SUCCEEDED collection books a customer receipt
    (Dr bank, Cr AR) and links it; FAILED/ABANDONED is recorded with no ledger effect.
    Re-confirming an already-terminal intent is a no-op (returns it unchanged).
    """
    intent = CollectionIntent.objects.select_for_update().get(pk=intent.pk)
    if intent.is_terminal:
        return intent

    if status is None:
        client = get_provider(intent.provider)
        result = client.verify_collection(
            reference=intent.reference, provider_reference=intent.provider_reference,
        )
        status = result.status
        amount = result.amount or intent.amount
        intent.raw_response = {**(intent.raw_response or {}), "verify": result.raw}

    if status != CollectionStatus.SUCCEEDED:
        intent.status = (CollectionStatus.FAILED if status == CollectionStatus.FAILED
                         else CollectionStatus.ABANDONED if status == CollectionStatus.ABANDONED
                         else intent.status)
        intent.save(update_fields=["status", "raw_response", "updated_at"])
        audit.record(
            action=PaymentAuditAction.COLLECTION_FAILED, entity=intent.entity,
            provider=intent.provider, reference=intent.reference, succeeded=False,
            message=f"Collection ended '{status}'.", actor_user=actor_user,
        )
        return intent

    _book_receipt(intent, actor_user=actor_user)
    intent.status = CollectionStatus.SUCCEEDED
    intent.confirmed_at = timezone.now()
    intent.save(update_fields=["status", "payment", "confirmed_at", "raw_response", "updated_at"])
    audit.record(
        action=PaymentAuditAction.COLLECTION_CONFIRMED, entity=intent.entity,
        provider=intent.provider, reference=intent.reference, actor_user=actor_user,
        message=f"Booked receipt for {intent.amount} kobo.",
        metadata={"payment_id": intent.payment_id},
    )
    return intent


def _book_receipt(intent, *, actor_user=None):
    """Create + post the ``vs_finance.Payment`` for a succeeded collection."""
    from vs_finance.models import Payment
    from vs_finance.receivables import post_payment

    if intent.customer_id is None:
        raise PaymentStateError(
            "Cannot book a receipt: the collection has no customer (AR sub-ledger).",
        )
    deposit = intent.deposit_account or resolve_account(
        intent.entity, CASH_BANK_CODE, label="Cash & bank",
    )
    payment = Payment.objects.create(
        entity=intent.entity, customer=intent.customer,
        payment_date=datetime.date.today(), currency=intent.currency,
        method=PaymentMethod.ONLINE, amount=intent.amount, deposit_account=deposit,
        reference=intent.reference,
        narration=intent.narration or f"Gateway collection {intent.reference}",
    )
    allocations = [(intent.invoice, intent.amount)] if intent.invoice_id else None
    post_payment(payment, actor_user=actor_user, allocations=allocations)
    intent.payment = payment


# --------------------------------------------------------------------------- #
# Payouts (money out)                                                          #
# --------------------------------------------------------------------------- #

def initiate_payout(*, entity, amount, beneficiary_name, beneficiary_account_number,
                    beneficiary_bank_code, vendor=None, source_account=None,
                    debit_account=None, provider=None, narration="", currency=None,
                    wht_amount=0, metadata=None, actor_user=None):
    """Create a :class:`PayoutInstruction` and ask the provider to transfer funds out.

    The ledger entry is booked only on *confirmation*, in one of two shapes:

    * **Vendor payout** — ``vendor`` is recorded as a loose reference so confirm can
      re-resolve and book a ``VendorPayment`` (Dr the vendor's AP control / Cr bank);
      the WHT split (``wht_amount``) flows through to that posting.
    * **Free-form payout** — no vendor, so there is no payable to settle; the operator
      nominates ``debit_account`` (an expense/clearing GL) and confirm books a direct
      bank disbursement (Dr that account / Cr the source bank). It is kept on metadata.
    """
    from django.conf import settings

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")
    client = get_provider(provider_name)
    reference = _new_reference()
    currency = currency or _entity_currency(entity)

    extra_meta = {"wht_amount": int(wht_amount)}
    if vendor is None and debit_account is not None:
        extra_meta["debit_account_id"] = debit_account.pk

    payout = PayoutInstruction.objects.create(
        entity=entity, provider=provider_name, reference=reference, amount=amount,
        currency=currency, beneficiary_name=beneficiary_name,
        beneficiary_account_number=beneficiary_account_number,
        beneficiary_bank_code=beneficiary_bank_code, source_account=source_account,
        narration=narration, status=PayoutStatus.PENDING,
        vendor_source_type="vs_procurement.Vendor" if vendor else "",
        vendor_source_id=str(vendor.pk) if vendor else "",
        metadata={**(metadata or {}), **extra_meta},
        created_by=actor_user,
    )
    return _dispatch_transfer(payout, client=client, metadata=metadata, actor_user=actor_user)


def _dispatch_transfer(payout, *, client=None, metadata=None, actor_user=None):
    """Ask the provider to transfer funds for an already-created ``PENDING`` payout.

    Shared by single :func:`initiate_payout` and bulk :func:`submit_payout_batch`. On
    provider rejection the payout is marked FAILED and the error re-raised; on success it
    moves to PROCESSING with the provider's reference/recipient stored. Booking the ledger
    entry still happens later, on confirmation.
    """
    client = client or get_provider(payout.provider)
    currency = payout.currency
    try:
        result = client.create_transfer(
            reference=payout.reference, amount=payout.amount,
            currency=getattr(currency, "code", currency) or "NGN",
            account_number=payout.beneficiary_account_number,
            bank_code=payout.beneficiary_bank_code,
            account_name=payout.beneficiary_name, narration=payout.narration,
            metadata=metadata or {},
        )
    except FinanceError as exc:
        payout.status = PayoutStatus.FAILED
        payout.failure_reason = str(getattr(exc, "message", exc))[:255]
        payout.save(update_fields=["status", "failure_reason", "updated_at"])
        audit.record_rejection(
            action=PaymentAuditAction.PAYOUT_INITIATED, exc=exc, entity=payout.entity,
            provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        )
        raise

    payout.provider_reference = result.provider_reference
    payout.recipient_code = result.recipient_code
    payout.status = PayoutStatus.PROCESSING
    payout.raw_response = result.raw
    payout.save(update_fields=[
        "provider_reference", "recipient_code", "status", "raw_response", "updated_at",
    ])
    audit.record(
        action=PaymentAuditAction.PAYOUT_INITIATED, entity=payout.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        message=f"Initiated {payout.amount} kobo payout via {payout.provider}.",
    )
    return payout


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
    from django.conf import settings

    items = list(items)
    if not items:
        raise PaymentStateError("A payout batch must contain at least one item.")

    provider_name = provider or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")
    get_provider(provider_name)  # validate the provider is configured up front
    currency = currency or _entity_currency(entity)
    batch_reference = _new_reference()

    with transaction.atomic():
        batch = PayoutBatch.objects.create(
            entity=entity, provider=provider_name, reference=batch_reference,
            title=title, narration=narration, currency=currency,
            source_account=source_account, status=PayoutBatchStatus.DRAFT,
            created_by=actor_user,
        )
        total = 0
        for item in items:
            amount = int(item.get("amount") or 0)
            if amount <= 0:
                raise PaymentStateError("Each payout item needs a positive amount (kobo).")
            vendor = item.get("vendor")
            PayoutInstruction.objects.create(
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
                metadata={**(item.get("metadata") or {}),
                          "wht_amount": int(item.get("wht_amount") or 0)},
                created_by=actor_user,
            )
            total += amount
        batch.total_amount = total
        batch.item_count = len(items)
        batch.save(update_fields=["total_amount", "item_count", "updated_at"])

    audit.record(
        action=PaymentAuditAction.PAYOUT_BATCH_CREATED, entity=entity,
        provider=provider_name, reference=batch_reference, actor_user=actor_user,
        message=f"Created payout batch of {len(items)} items, {total} kobo.",
    )
    return batch


def submit_payout_batch(batch, *, actor_user=None):
    """Submit every ``PENDING`` instruction in ``batch`` to the provider, one by one.

    Each item rides the shared :func:`_dispatch_transfer`; a per-item provider rejection
    marks that instruction FAILED but does not abort the run. The batch's aggregate status
    is recomputed from its children afterwards. Idempotent: already-dispatched items
    (non-PENDING) are skipped, so re-submitting a partially-failed batch only retries the
    stragglers.
    """
    submitted = failed = 0
    for payout in batch.instructions.filter(status=PayoutStatus.PENDING):
        try:
            _dispatch_transfer(payout, actor_user=actor_user)
            submitted += 1
        except FinanceError:
            failed += 1

    batch.submitted_at = batch.submitted_at or timezone.now()
    _recompute_batch_status(batch)
    audit.record(
        action=PaymentAuditAction.PAYOUT_BATCH_SUBMITTED, entity=batch.entity,
        provider=batch.provider, reference=batch.reference, actor_user=actor_user,
        message=f"Submitted batch: {submitted} dispatched, {failed} failed.",
        metadata={"submitted": submitted, "failed": failed},
    )
    return batch


def _recompute_batch_status(batch):
    """Derive and persist the batch status from the live state of its instructions."""
    statuses = list(
        batch.instructions.values_list("status", flat=True)
    )
    total = len(statuses)
    paid = sum(1 for s in statuses if s == PayoutStatus.PAID)
    failed = sum(1 for s in statuses if s in (PayoutStatus.FAILED, PayoutStatus.REVERSED))
    pending = sum(1 for s in statuses if s == PayoutStatus.PENDING)
    in_flight = sum(1 for s in statuses if s == PayoutStatus.PROCESSING)

    if total == 0:
        status = PayoutBatchStatus.DRAFT
    elif pending == total:
        status = PayoutBatchStatus.DRAFT
    elif paid == total:
        status = PayoutBatchStatus.COMPLETED
    elif failed == total:
        status = PayoutBatchStatus.FAILED
    elif pending or in_flight:
        status = PayoutBatchStatus.PROCESSING
    else:
        # Everything settled, but a mix of paid and failed.
        status = PayoutBatchStatus.PARTIALLY_COMPLETED

    if batch.status != status or batch.submitted_at is not None:
        batch.status = status
        batch.save(update_fields=["status", "submitted_at", "updated_at"])
    return batch


@transaction.atomic
def confirm_payout(payout, *, status=None, actor_user=None):
    """Confirm a payout and book the vendor payment — idempotently."""
    payout = PayoutInstruction.objects.select_for_update().get(pk=payout.pk)
    if payout.is_terminal:
        return payout

    if status is None:
        client = get_provider(payout.provider)
        result = client.verify_transfer(
            reference=payout.reference, provider_reference=payout.provider_reference,
        )
        status = result.status
        payout.raw_response = {**(payout.raw_response or {}), "verify": result.raw}

    if status != PayoutStatus.PAID:
        if status in (PayoutStatus.FAILED, PayoutStatus.REVERSED):
            payout.status = status
        payout.save(update_fields=["status", "raw_response", "updated_at"])
        audit.record(
            action=PaymentAuditAction.PAYOUT_FAILED, entity=payout.entity,
            provider=payout.provider, reference=payout.reference, succeeded=False,
            message=f"Payout ended '{status}'.", actor_user=actor_user,
        )
        _refresh_batch(payout)
        return payout

    # A payout settles either a vendor's payable (Dr AP / Cr bank) or — for a
    # free-form recipient with no vendor — a direct bank disbursement against the
    # operator-nominated GL account (Dr that account / Cr bank).
    if payout.vendor_source_id:
        _book_vendor_payment(payout, actor_user=actor_user)
        booked_msg = f"Booked vendor payment for {payout.amount} kobo."
        booked_meta = {"vendor_payment_id": payout.vendor_payment_id}
    else:
        _book_generic_disbursement(payout, actor_user=actor_user)
        booked_msg = f"Booked bank disbursement for {payout.amount} kobo."
        booked_meta = {"journal_entry_id": (payout.metadata or {}).get("journal_entry_id")}
    payout.status = PayoutStatus.PAID
    payout.confirmed_at = timezone.now()
    payout.save(update_fields=[
        "status", "vendor_payment_id", "confirmed_at", "raw_response", "metadata", "updated_at",
    ])
    audit.record(
        action=PaymentAuditAction.PAYOUT_CONFIRMED, entity=payout.entity,
        provider=payout.provider, reference=payout.reference, actor_user=actor_user,
        message=booked_msg, metadata=booked_meta,
    )
    _refresh_batch(payout)
    return payout


def _refresh_batch(payout):
    """Recompute the owning batch's aggregate status after a child changed, if any."""
    if payout.batch_id:
        _recompute_batch_status(
            PayoutBatch.objects.select_for_update().get(pk=payout.batch_id)
        )


def _book_vendor_payment(payout, *, actor_user=None):
    """Create + post the ``vs_procurement.VendorPayment`` for a paid payout."""
    if not payout.vendor_source_id:
        raise PaymentStateError(
            "Cannot book a vendor payment: the payout has no vendor reference.",
        )
    from vs_procurement.models import Vendor, VendorPayment
    from vs_procurement.payables import post_vendor_payment

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
    )
    post_vendor_payment(vp, actor_user=actor_user)
    payout.vendor_payment_id = vp.pk


def _book_generic_disbursement(payout, *, actor_user=None):
    """Book a free-form (non-vendor) payout as a direct bank disbursement.

    No vendor payable exists, so the operator nominates the GL account to debit
    (an expense or clearing account) when creating the payout. We post
    Dr <debit account> / Cr <source bank>; the journal id is kept on metadata so
    the gateway record links back to the ledger.
    """
    from vs_finance.constants import JournalSource
    from vs_finance.models import Account, JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    debit_id = (payout.metadata or {}).get("debit_account_id")
    debit = Account.objects.filter(entity=payout.entity, pk=debit_id).first() if debit_id else None
    if debit is None:
        raise PaymentStateError(
            "Cannot book this payout: a free-form (non-vendor) payout needs a debit "
            "(expense/clearing) account to post against.",
        )
    source = payout.source_account or resolve_account(
        payout.entity, CASH_BANK_CODE, label="Cash & bank",
    )
    today = datetime.date.today()
    entry = JournalEntry.objects.create(
        entity=payout.entity, date=today, period=resolve_period(payout.entity, today),
        source=JournalSource.BANK, currency=payout.currency,
        narration=payout.narration or f"Payout {payout.reference}",
        reference=payout.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=debit, debit=payout.amount, credit=0,
        description=f"Payout: {payout.beneficiary_name}"[:255], line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=source, debit=0, credit=payout.amount,
        description=f"Disbursement {payout.reference}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)
    payout.metadata = {**(payout.metadata or {}), "journal_entry_id": entry.pk}
