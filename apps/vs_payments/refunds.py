"""Returning a customer's online payment to the payer through the payment provider.

A refund in ``vs_finance`` pays a customer's stored credit back out, normally
from a bank account. For a tenant whose online money the platform holds
(:mod:`vs_payments.custody`), the money a parent paid online is in the provider
balance, so it can go back the way it came. This module is that path, and it
reuses the finance refund's own life (draft, approval, post) rather than adding
a second one.

How one refund runs
-------------------

Mrs Adeyemi paid Bright Star Lekki N180,000 online; Tunde is then withdrawn and
the bursar raises a N100,000 refund of her credit, method *Online*.

1. **Tie it to the payment** (:func:`link_refund`). The refund names the
   confirmed online payment it returns. It must be the same customer and the
   same branch, and no more than what the payment still has left after earlier
   refunds through the provider.
2. **Post it** through the finance refund's usual route, with or without the
   approval the tenant has set. Posting finds the refund tied to a payment and
   hands it to :class:`ProviderRefundChannel` instead of posting a bank journal
   (:func:`vs_finance.credit_notes.register_refund_channel`). In that
   transaction the channel locks the payment, reserves N100,000 of Lekki's held
   balance (refused when Lekki holds less, so the refund stays a draft), and
   arranges for the provider to be asked once the transaction commits. The
   refund waits as ``APPROVED``, still reserving the customer's credit.
3. **The provider returns it** (:func:`send_provider_refund`, off the request,
   in a worker). Its answer arrives by ``refund.*`` event and by the recovery
   sweep; either way it is re-verified through the provider's API before the
   books move (:func:`confirm_provider_refund`).
4. **Processed**: the tenant's books record ``Dr customer credit, Cr gateway
   clearing`` (the money left the provider balance, not a bank) and the refund is
   ``POSTED``. The held reduction taken in step 2 stands.
   **Failed**: the reservation is released, the refund returns to a draft, and
   the bursar sends it again or pays it from the branch's own bank
   (:func:`pay_from_bank`).

The provider does not give back the fee it kept from the original payment when
it refunds it, so nothing is booked for it: that fee stays the branch's bank
charge as it was booked when the payment settled.

A tenant whose payments settle directly to its branches' banks never refunds
this way (:func:`vs_payments.custody.assert_online_refund_allowed`): the money
is in the branch's bank, and a refund through the provider would be paid out of
the platform's own balance. A refund made at the provider's dashboard is matched
to nothing here and is recorded and raised to people
(:func:`vs_payments.held.record_dispute`).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_finance.exceptions import FinanceError
from vs_finance.money import format_naira

from . import audit, custody, held
from .constants import (
    PROVIDER_REFUND_COUNTS,
    CollectionStatus,
    PaymentAuditAction,
    ProviderRefundStatus,
)
from .exceptions import PaymentStateError, ProviderNotConfiguredError
from .models import CollectionIntent, ProviderRefund
from .providers.registry import get_provider

logger = logging.getLogger("vs_payments.refunds")


# --------------------------------------------------------------------------- #
# Tying a refund to the payment it returns                                     #
# --------------------------------------------------------------------------- #

def refundable_through_provider(intent, *, excluding=None) -> int:
    """Kobo of ``intent`` that can still be returned to its payer through the provider.

    The payment's amount less every refund of it that is tied, sent or done.
    ``excluding`` is a refund link left out of the sum, the one being checked.
    """
    rows = ProviderRefund.objects.filter(
        collection=intent, status__in=PROVIDER_REFUND_COUNTS)
    if excluding is not None:
        rows = rows.exclude(pk=excluding)
    return int(intent.amount) - int(rows.aggregate(total=Sum("amount"))["total"] or 0)


def _check_source(entity, refund, intent) -> None:
    """Refuse a payment the refund cannot return, naming why (400 for the data, 409 for the state)."""
    from vs_rbac.scoping import same_transaction_branch

    from .providers.registry import is_available

    custody.assert_online_refund_allowed(entity)
    if intent.entity_id != entity.pk:
        raise ValidationError({"collection": "No such collection in this entity."})
    if intent.customer_id != refund.customer_id:
        raise ValidationError({"collection": (
            "This payment was made for a different customer than the refund is for.")})
    if not same_transaction_branch(entity.tenant_id, intent.branch_id, refund.branch_id):
        raise ValidationError({"collection": (
            "This payment belongs to a different branch than the refund. A refund returns "
            "only its own branch's money.")})
    if intent.status != CollectionStatus.SUCCEEDED:
        raise PaymentStateError("Only a payment the provider has confirmed can be refunded.")
    if not is_available(intent.provider):
        raise PaymentStateError("This payment's provider is not offered here, so it cannot refund it.")
    if not intent.held_by_platform:
        raise PaymentStateError(
            "This payment settled straight to the branch's bank, not to the platform's "
            "balance, so it cannot be returned through the provider. Pay the refund from "
            "the branch's bank and record it.")


def link_refund(*, entity, refund, collection, actor_user=None) -> ProviderRefund:
    """Tie a draft online refund to the confirmed payment it returns.

    The refund must be a draft raised with method *Online / Gateway*. Under a lock
    on the payment, the refund may not exceed what is left of it
    (:func:`refundable_through_provider`), which is what stops two refunds being
    tied to one payment from together returning more than was paid. A refund
    already tied to another payment is re-tied: the earlier tie is cancelled.
    """
    from vs_finance.constants import DocumentStatus, PaymentMethod
    from vs_finance.models import Refund

    with transaction.atomic():
        intent = CollectionIntent.objects.select_for_update().get(pk=collection.pk)
        refund = Refund.objects.select_for_update().get(pk=refund.pk)
        if refund.entity_id != entity.pk:
            raise ValidationError({"refund": "No such refund in this entity."})
        if refund.status != DocumentStatus.DRAFT:
            raise PaymentStateError(
                "Only a draft refund can be tied to a payment; this one is "
                f"{refund.get_status_display().lower()}.")
        if refund.method != PaymentMethod.ONLINE:
            raise ValidationError({"refund": (
                "Raise the refund with the method Online / Gateway to return it through "
                "the provider.")})
        _check_source(entity, refund, intent)
        earlier = list(ProviderRefund.objects.select_for_update().filter(
            refund=refund, status=ProviderRefundStatus.LINKED))
        left = refundable_through_provider(
            intent, excluding=earlier[0].pk if earlier else None)
        if int(refund.amount) > left:
            raise ValidationError({"amount": (
                f"This payment of {format_naira(intent.amount)} has {format_naira(max(left, 0))} "
                f"left to refund through the provider; the refund is {format_naira(refund.amount)}.")})
        for row in earlier:
            row.status = ProviderRefundStatus.CANCELLED
            row.save(update_fields=["status", "updated_at"])
        link = ProviderRefund.objects.create(
            entity=entity, branch_id=refund.branch_id, refund=refund, collection=intent,
            provider=intent.provider, amount=refund.amount,
            transaction_reference=intent.reference, created_by=actor_user,
        )
    audit.record(
        action=PaymentAuditAction.PROVIDER_REFUND_LINKED, entity=entity,
        provider=intent.provider, reference=intent.reference, actor_user=actor_user,
        message=(f"Refund {refund.document_number or refund.pk} of "
                 f"{format_naira(refund.amount)} tied to payment {intent.reference}."),
        metadata={"provider_refund_id": link.pk, "refund_id": refund.pk,
                  "collection_id": intent.pk, "branch_id": refund.branch_id},
    )
    return link


def pay_from_bank(link, *, method, bank_account=None, actor_user=None):
    """Stop returning a refund through the provider and pay it from a bank account instead.

    For a refund whose tie was never sent, or whose provider attempt failed: it is
    a draft again, and this drops the tie and gives the refund a bank method and
    account (``bank_account``, already checked to be the refund's own branch's),
    ready to post as any bank refund. A refund the provider is handling or has
    returned is refused (409).
    """
    from vs_finance.constants import DocumentStatus, PaymentMethod
    from vs_finance.credit_notes import check_refund_method
    from vs_finance.models import Refund

    with transaction.atomic():
        link = ProviderRefund.objects.select_for_update().get(pk=link.pk)
        if link.status not in (ProviderRefundStatus.LINKED, ProviderRefundStatus.FAILED):
            raise PaymentStateError(
                "Only a refund that has not been sent, or that the provider failed, can be "
                "paid from the bank instead.")
        refund = Refund.objects.select_for_update().get(pk=link.refund_id)
        if refund.status != DocumentStatus.DRAFT:
            raise PaymentStateError(
                f"The refund is {refund.get_status_display().lower()}, not a draft.")
        if str(method or "").upper() == PaymentMethod.ONLINE:
            raise ValidationError({"method": "Choose a method other than Online / Gateway."})
        check_refund_method(link.entity, method)
        if bank_account is None and not (refund.bank_account_id or refund.deposit_account_id):
            raise ValidationError({"bank_account": "Name the bank account the refund is paid from."})
        if link.status == ProviderRefundStatus.LINKED:
            link.status = ProviderRefundStatus.CANCELLED
            link.save(update_fields=["status", "updated_at"])
        refund.method = method
        if bank_account is not None:
            refund.bank_account = bank_account
        refund.save(update_fields=["method", "bank_account", "updated_at"])
    audit.record(
        action=PaymentAuditAction.PROVIDER_REFUND_CANCELLED, entity=link.entity,
        provider=link.provider, reference=link.transaction_reference, actor_user=actor_user,
        message=f"Refund {refund.document_number or refund.pk} will be paid from the bank instead.",
        metadata={"provider_refund_id": link.pk, "refund_id": refund.pk,
                  "branch_id": link.branch_id},
    )
    return link


# --------------------------------------------------------------------------- #
# The channel finance hands a posted refund to                                  #
# --------------------------------------------------------------------------- #

class ProviderRefundChannel:
    """Pays a refund tied to an online payment through the provider, registered with finance.

    See :func:`vs_finance.credit_notes.register_refund_channel` for the contract
    and the module docstring for the whole path.
    """

    def carries(self, refund) -> bool:
        return ProviderRefund.objects.filter(
            refund_id=refund.pk, status=ProviderRefundStatus.LINKED).exists()

    def preflight(self, refund) -> None:
        """Refuse a refund the provider cannot be asked to return, changing nothing (409/400)."""
        link = ProviderRefund.objects.select_related("collection", "entity").get(
            refund_id=refund.pk, status=ProviderRefundStatus.LINKED)
        entity = link.entity
        _check_source(entity, refund, link.collection)
        left = refundable_through_provider(link.collection, excluding=link.pk)
        if int(refund.amount) > left:
            raise PaymentStateError(
                f"The payment has {format_naira(max(left, 0))} left to refund through the "
                f"provider; this refund is {format_naira(refund.amount)}.")
        if int(link.amount) != int(refund.amount):
            raise PaymentStateError(
                "The refund's amount changed after it was tied to the payment; tie it again.")
        branch_id = held.held_branch_id(entity, link.branch_id)
        if held.held_balance(branch_id) < int(refund.amount):
            raise held.InsufficientHeldFundsError(
                f"The branch holds {format_naira(max(held.held_balance(branch_id), 0))} of online "
                f"payments, and this refund needs {format_naira(refund.amount)}. Pay it from the "
                f"branch's bank instead.")

    def begin(self, refund, *, actor_user=None) -> None:
        """Reserve the money and arrange for the provider to be asked after commit."""
        link = ProviderRefund.objects.select_for_update().select_related(
            "collection", "entity").get(refund_id=refund.pk, status=ProviderRefundStatus.LINKED)
        intent = CollectionIntent.objects.select_for_update().get(pk=link.collection_id)
        link.collection = intent
        entity = link.entity
        _check_source(entity, refund, intent)
        left = refundable_through_provider(intent, excluding=link.pk)
        if int(refund.amount) > left:
            raise PaymentStateError(
                f"The payment has {format_naira(max(left, 0))} left to refund through the "
                f"provider; this refund is {format_naira(refund.amount)}.")
        if int(link.amount) != int(refund.amount):
            raise PaymentStateError(
                "The refund's amount changed after it was tied to the payment; tie it again.")
        held.reserve_for_refund(link, actor_user=actor_user)
        link.status = ProviderRefundStatus.PROCESSING
        link.save(update_fields=["status", "updated_at"])
        audit.record(
            action=PaymentAuditAction.PROVIDER_REFUND_SENT, entity=entity,
            provider=link.provider, reference=intent.reference, actor_user=actor_user,
            message=(f"Refund of {format_naira(link.amount)} reserved from the branch's held "
                     f"money and queued for {link.provider}."),
            metadata={"provider_refund_id": link.pk, "refund_id": refund.pk,
                      "collection_id": intent.pk, "branch_id": link.branch_id,
                      "amount": int(link.amount)},
        )
        link_id = link.pk
        transaction.on_commit(lambda: _enqueue_send(link_id))

    def void_blocker(self, refund) -> str:
        if ProviderRefund.objects.filter(
                refund_id=refund.pk, status=ProviderRefundStatus.PROCESSED).exists():
            return (f"Refund {refund.document_number or refund.pk} was returned to the payer "
                    f"through the payment provider, so voiding it would put back credit the "
                    f"payer has already been paid.")
        return ""


channel = ProviderRefundChannel()


def _enqueue_send(link_id) -> None:
    """Hand a posted refund to a worker; a broker problem never undoes the post."""
    from .tasks import send_provider_refund_task

    try:
        send_provider_refund_task.delay(link_id)  # Inline under ALWAYS_EAGER.
    except Exception:  # noqa: BLE001 - the post is durable and the recovery sweep re-sends.
        logger.exception("Could not enqueue the provider refund %s.", link_id)


# --------------------------------------------------------------------------- #
# Asking the provider, and hearing back                                        #
# --------------------------------------------------------------------------- #

def send_provider_refund(link_id) -> ProviderRefund | None:
    """Ask the provider to return a posted refund, once, outside any transaction.

    The refund claims itself (``sent_at``) in its own short transaction before the
    provider is asked, so a retried task or a second worker cannot ask twice. A
    clean rejection fails the refund and releases its reservation; a timeout or a
    server error says nothing about whether the provider acted, so the refund
    stays in flight for the webhook or the recovery sweep to settle.
    """
    from .services import _assert_no_open_transaction, _provider_failure_is_final

    _assert_no_open_transaction("Sending a refund to the provider")
    with transaction.atomic():
        link = ProviderRefund.objects.select_for_update().select_related(
            "refund", "entity").filter(pk=link_id).first()
        if (link is None or link.status != ProviderRefundStatus.PROCESSING
                or link.sent_at is not None or link.provider_reference):
            return link
        link.sent_at = timezone.now()
        link.save(update_fields=["sent_at", "updated_at"])
    try:
        client = get_provider(link.provider)
        result = client.create_refund(
            transaction_reference=link.transaction_reference, amount=int(link.amount),
            currency=getattr(link.entity.base_currency, "code", None) or "NGN",
            customer_note=link.refund.narration or "",
            merchant_note=f"Refund {link.refund.document_number or link.refund_id}",
        )
    except ProviderNotConfiguredError as exc:  # Nothing was sent.
        return _fail(link.pk, str(getattr(exc, "message", exc)))
    except FinanceError as exc:
        if _provider_failure_is_final(exc):
            return _fail(link.pk, str(getattr(exc, "message", exc)))
        logger.warning("Provider refund %s: the outcome of sending is unknown: %s", link.pk, exc)
        return link
    return _apply_result(link.pk, result)


def confirm_provider_refund(link) -> ProviderRefund:
    """Re-verify a refund in flight with the provider and book what it reports; idempotent."""
    link = ProviderRefund.objects.get(pk=link.pk)
    if link.status != ProviderRefundStatus.PROCESSING or link.sent_at is None:
        return link
    result = get_provider(link.provider).verify_refund(
        provider_reference=link.provider_reference,
        transaction_reference=link.transaction_reference, amount=int(link.amount),
    )
    return _apply_result(link.pk, result)


def refunds_in_flight(collection):
    """The refunds of ``collection`` posted and not yet finished by the provider.

    One whose request is still being sent counts: an event about it can arrive
    before the provider's reply is saved, and it is ours all the same.
    """
    return ProviderRefund.objects.filter(
        collection=collection, status=ProviderRefundStatus.PROCESSING)


def confirm_refunds_of(collection, parsed=None) -> int:
    """Re-verify every refund in flight of ``collection``; the number an event of ours concerns.

    A ``refund.*`` event names the payment, not our refund, so each refund in
    flight for it is asked about and the provider's answer decides. An event about
    a refund already finished is ours as well when it carries the amount of one
    (a re-delivery); anything else concerns a refund made outside this system,
    and the caller records and raises it. Returns 0 for such an event.
    """
    in_flight = list(refunds_in_flight(collection))
    for link in in_flight:
        confirm_provider_refund(link)
    if in_flight:
        return len(in_flight)
    amount = int(getattr(parsed, "amount", 0) or 0)
    finished = ProviderRefund.objects.filter(
        collection=collection,
        status__in=(ProviderRefundStatus.PROCESSED, ProviderRefundStatus.FAILED))
    if amount and finished.filter(amount=amount).exists():
        return 1
    return 0


@transaction.atomic
def _apply_result(link_pk, result) -> ProviderRefund:
    """Record the provider's answer on the refund, booking or releasing it when final."""
    link = ProviderRefund.objects.select_for_update().select_related(
        "refund", "entity", "collection").get(pk=link_pk)
    if link.status != ProviderRefundStatus.PROCESSING:
        return link
    if result.provider_reference and not link.provider_reference:
        link.provider_reference = str(result.provider_reference)[:128]
    link.provider_status = (result.provider_status or link.provider_status)[:32]
    link.raw_response = {"last": result.raw}
    if result.status == ProviderRefundStatus.PROCESSED:
        if result.amount and int(result.amount) != int(link.amount):
            raise PaymentStateError(
                f"The provider reports returning {format_naira(result.amount)} for a refund of "
                f"{format_naira(link.amount)}; nothing was booked. Check the refund at the provider.")
        _book_processed(link)
    elif result.status == ProviderRefundStatus.FAILED:
        _release_failed(link, result.failure_reason or "The provider could not return the money.")
    else:
        link.save(update_fields=["provider_reference", "provider_status", "raw_response", "updated_at"])
    return link


def _fail(link_pk, reason) -> ProviderRefund:
    """Fail a refund the provider rejected outright, releasing what it reserved."""
    with transaction.atomic():
        link = ProviderRefund.objects.select_for_update().select_related(
            "refund", "entity", "collection").get(pk=link_pk)
        if link.status == ProviderRefundStatus.PROCESSING:
            _release_failed(link, reason)
    return link


def _book_processed(link) -> None:
    """The provider returned the money: Dr customer credit, Cr gateway clearing, once.

    Dated the day it is booked, or the first open day after when that day's
    period is closed. The provider's fee on the original payment is not refunded
    and nothing is booked for it. The held reduction taken when the refund was
    posted stands.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.banking import resolve_adjustment_date
    from vs_finance.constants import AccountMappingKey
    from vs_finance.credit_notes import finish_channel_refund

    from .services import _booking_date

    entity = link.entity
    clearing = resolve_mapped_account(
        entity, AccountMappingKey.GATEWAY_CLEARING, label="gateway clearing")
    today, _ = _booking_date(entity, None, branch=link.branch_id)
    on = resolve_adjustment_date(entity, today, branch=link.branch_id)
    refund = link.refund
    finish_channel_refund(
        refund, source_account=clearing, on=on,
        message=f"Refunded {refund.customer.code} through {link.provider}.",
    )
    link.status = ProviderRefundStatus.PROCESSED
    link.processed_at = timezone.now()
    link.save(update_fields=[
        "status", "processed_at", "provider_reference", "provider_status", "raw_response",
        "updated_at",
    ])
    audit.record(
        action=PaymentAuditAction.PROVIDER_REFUND_PROCESSED, entity=entity,
        provider=link.provider, reference=link.transaction_reference,
        message=(f"{link.provider} returned {format_naira(link.amount)} of payment "
                 f"{link.transaction_reference} to the payer."),
        metadata={"provider_refund_id": link.pk, "refund_id": link.refund_id,
                  "collection_id": link.collection_id, "branch_id": link.branch_id,
                  "amount": int(link.amount)},
    )


def _release_failed(link, reason) -> None:
    """The provider will not return the money: free the reservation and reopen the draft."""
    from vs_finance.credit_notes import return_channel_refund_to_draft

    held.release_refund(link, reason=reason)
    return_channel_refund_to_draft(link.refund)
    link.status = ProviderRefundStatus.FAILED
    link.failure_reason = str(reason or "")[:255]
    link.save(update_fields=[
        "status", "failure_reason", "provider_reference", "provider_status", "raw_response",
        "updated_at",
    ])
    audit.record(
        action=PaymentAuditAction.PROVIDER_REFUND_FAILED, entity=link.entity,
        provider=link.provider, reference=link.transaction_reference, succeeded=False,
        message=f"Refund of {format_naira(link.amount)} failed: {link.failure_reason}"[:255],
        metadata={"provider_refund_id": link.pk, "refund_id": link.refund_id,
                  "collection_id": link.collection_id, "branch_id": link.branch_id,
                  "amount": int(link.amount)},
    )
