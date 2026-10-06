"""Customer credit transfers: the one approved way money moves between customers.

A receipt settles only its own customer's documents
(:func:`vs_finance.receivables._require_settlable_targets`). When a family asks for
one child's overpayment to pay a sibling's fees, the credit is moved by a
:class:`~vs_finance.models.CustomerCreditTransfer`, which a second person approves
through the workflow engine and which is audited end to end. See the model for how
the transfer is carried in the sub-ledger and the GL.
"""
from __future__ import annotations

from django.db import transaction

from vs_config.display import format_date

from .account_mappings import resolve_mapped_account
from .audit import record, record_rejection
from .constants import (
    CREDIT_TRANSFER_METHOD, AccountMappingKey, DocumentStatus, FinanceAuditAction,
)
from .exceptions import FinanceError, PostingError
from .money import format_naira
from .wording import state_word


def check_transfer(transfer):
    """Refuse a transfer that could never post, without writing anything.

    The two customers must differ and share the transfer's books; each must be
    filed under the transfer's branch or shared by every branch, because the credit
    leaves and arrives in that branch's books; and the source must hold at least
    ``amount`` of refundable credit in that branch on the transfer's own date. Credit
    reserved by a refund awaiting approval, or owed back on an unsettled debit note,
    is not available to transfer.
    """
    from .receivables import customer_refund_available_balance, require_refund_branch_credit

    source, destination = transfer.from_customer, transfer.to_customer
    if transfer.amount <= 0:
        raise PostingError("A credit transfer must move a positive amount.")
    if source.pk == destination.pk:
        raise PostingError("A credit transfer moves credit between two different customers.")
    if source.entity_id != transfer.entity_id or destination.entity_id != transfer.entity_id:
        raise PostingError("Both customers of a credit transfer must be in its books.")
    for customer in (source, destination):
        if customer.branch_id is not None and customer.branch_id != transfer.branch_id:
            raise PostingError(
                f"{customer.code} is filed under another branch than this transfer's; "
                f"credit moves only within one branch's books.",
            )
    if destination.receivable_account_id is None:
        raise PostingError(f"Customer {destination.code} has no receivable (AR control) account set.")
    available = customer_refund_available_balance(
        source, as_of=transfer.transfer_date, branch=transfer.branch_id)
    if transfer.amount > available:
        require_refund_branch_credit(
            source, transfer.amount, transfer.branch_id, as_of=transfer.transfer_date)
        raise PostingError(
            f"{source.code} holds {format_naira(available)} of credit available on "
            f"{format_date(transfer.transfer_date, transfer.entity.tenant)}, less than "
            f"the {format_naira(transfer.amount)} this transfer moves.",
        )


def post_customer_credit_transfer(transfer, *, actor_user=None):
    """Post an approved transfer, recording a durable rejection audit row when refused."""
    try:
        return _post_transfer_atomic(transfer, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=transfer.entity, action=FinanceAuditAction.CREDIT_TRANSFER_POSTED,
            exc=exc, actor_user=actor_user, target=transfer,
        )
        raise


@transaction.atomic
def _post_transfer_atomic(transfer, *, actor_user=None):
    """Draw the source's credit lots and give the destination a credit-transfer receipt.

    Only an APPROVED transfer posts: there is no direct route, so every transfer has
    been through a second person. Both customers are locked in id order, as a refund
    locks its customer, so a refund or a second transfer drawing the same credit
    queues behind this one and then finds it spent.
    """
    from .chronology import credit_lots, plan_credit_draw
    from .document_settings import resolve_finance_document_settings
    from .models import (
        CreditNote, Customer, CustomerCreditTransfer, CustomerCreditTransferDraw, Payment,
    )
    from .receivables import _post_payment_atomic

    CustomerCreditTransfer.objects.select_for_update(of=("self",)).get(pk=transfer.pk)
    transfer.refresh_from_db()
    if transfer.status != DocumentStatus.APPROVED:
        raise PostingError(
            f"Credit transfer {transfer.document_number or transfer.pk} is "
            f"{state_word(transfer)}; only an approved transfer can be posted.",
        )
    list(Customer.objects.select_for_update().filter(
        pk__in=sorted({transfer.from_customer_id, transfer.to_customer_id})).order_by("pk"))
    check_transfer(transfer)

    lots = credit_lots(
        transfer.entity, [transfer.from_customer_id],
        as_of=transfer.transfer_date, branch=transfer.branch_id,
    ).get(transfer.from_customer_id, [])
    plan = plan_credit_draw(lots, transfer.amount)
    if sum(taken for _lot, taken in plan) < transfer.amount:
        raise PostingError(
            f"{transfer.from_customer.code}'s credit could not be matched to this "
            f"{format_naira(transfer.amount)} transfer. Refresh the customer's credit and "
            f"try again.",
        )
    drawn_from = []
    for lot, taken in plan:  # Record each draw and drain its source lot.
        if lot.kind == "RECEIPT":
            source = Payment.objects.select_for_update(of=("self",)).get(pk=lot.document_id)
            CustomerCreditTransferDraw.objects.create(transfer=transfer, payment=source, amount=taken)
        else:
            source = CreditNote.objects.select_for_update(of=("self",)).get(pk=lot.document_id)
            CustomerCreditTransferDraw.objects.create(transfer=transfer, note=source, amount=taken)
        source.transferred_amount += taken
        source.save(update_fields=["transferred_amount", "updated_at"])
        drawn_from.append({"source": source.document_number, "amount": taken})

    receipt = Payment.objects.create(
        entity=transfer.entity, customer=transfer.to_customer, branch_id=transfer.branch_id,
        payment_date=transfer.transfer_date, method=CREDIT_TRANSFER_METHOD,
        amount=transfer.amount,
        deposit_account=resolve_mapped_account(
            transfer.entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),
        reference=transfer.document_number or "",
        narration=f"Credit transferred from {transfer.from_customer.code}",
        created_by=actor_user,
    )
    settles = resolve_finance_document_settings(transfer.entity).auto_apply_customer_credit
    _post_payment_atomic(receipt, actor_user=actor_user, auto_allocate=settles)

    transfer.receipt = receipt
    transfer.status = DocumentStatus.POSTED
    transfer.save(update_fields=["receipt", "status", "updated_at"])
    record(
        entity=transfer.entity, action=FinanceAuditAction.CREDIT_TRANSFER_POSTED,
        actor_user=actor_user, target=transfer,
        message=(
            f"Moved {format_naira(transfer.amount)} of credit from "
            f"{transfer.from_customer.code} to {transfer.to_customer.code}."
        ),
        amount=transfer.amount, receipt=receipt.document_number,
        journal_id=receipt.journal_id, drawn_from=drawn_from,
        applied=receipt.allocated_amount,
    )
    return transfer


def void_customer_credit_transfer(transfer, *, actor_user=None, date=None):
    """Void a posted transfer, recording a durable rejection audit row when refused."""
    try:
        return _void_transfer_atomic(transfer, actor_user=actor_user, date=date)
    except FinanceError as exc:
        record_rejection(
            entity=transfer.entity, action=FinanceAuditAction.CREDIT_TRANSFER_REVERSED,
            exc=exc, actor_user=actor_user, target=transfer,
        )
        raise


@transaction.atomic
def _void_transfer_atomic(transfer, *, actor_user=None, date=None):
    """Undo a transfer: void its receipt, then give the credit back to its source lots.

    Voiding the receipt reopens whatever bills of the destination it settled and
    reverses its journal. It is refused once the destination has refunded or
    transferred the credit on, because that money has left the destination and
    giving it back to the source would count it twice.
    """
    from .models import CreditNote, CustomerCreditTransfer, Payment
    from .voids import _void_payment_atomic

    CustomerCreditTransfer.objects.select_for_update(of=("self",)).get(pk=transfer.pk)
    transfer.refresh_from_db()
    if transfer.status != DocumentStatus.POSTED or transfer.receipt_id is None:
        raise PostingError(
            f"Only a posted credit transfer can be voided; "
            f"{transfer.document_number or transfer.pk} is {state_word(transfer)}.",
        )
    _void_payment_atomic(transfer.receipt, actor_user=actor_user, date=date, transfer=transfer)

    draws = list(transfer.draws.order_by("payment_id", "note_id", "pk"))
    payments = {p.pk: p for p in Payment.objects.select_for_update(of=("self",)).filter(
        pk__in=sorted({d.payment_id for d in draws if d.payment_id})).order_by("pk")}
    notes = {n.pk: n for n in CreditNote.objects.select_for_update(of=("self",)).filter(
        pk__in=sorted({d.note_id for d in draws if d.note_id})).order_by("pk")}
    for draw in draws:  # The credit returns to the lots it came from.
        source = payments[draw.payment_id] if draw.payment_id else notes[draw.note_id]
        source.transferred_amount = max(0, source.transferred_amount - draw.amount)
        source.save(update_fields=["transferred_amount", "updated_at"])

    transfer.status = DocumentStatus.REVERSED
    transfer.save(update_fields=["status", "updated_at"])
    record(
        entity=transfer.entity, action=FinanceAuditAction.CREDIT_TRANSFER_REVERSED,
        actor_user=actor_user, target=transfer,
        message=f"Voided credit transfer {transfer.document_number}.",
        receipt=transfer.receipt.document_number,
    )
    return transfer
