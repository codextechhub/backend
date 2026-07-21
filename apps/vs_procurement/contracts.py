"""Vendor-contract services — term agreements, milestones and renewal alerts.

A master-data overlay with **no GL effect**: a :class:`~vs_procurement.models.VendorContract`
records the commercial envelope (period, value, payment terms) and an optional list of
:class:`~vs_procurement.models.ContractMilestone` s. These services drive the contract
lifecycle (activate → renew / terminate / expire), tick off milestones, and surface the
contracts a buyer should chase for renewal before they lapse. All money is integer kobo;
nothing here touches the ledger.
"""
from __future__ import annotations

import datetime

from django.db import transaction

from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction

from .constants import CONTRACT_DOC_TYPE, ContractStatus, MilestoneStatus
from .exceptions import ContractError
from .purchasing import vendor_purchase_block_reason


# --------------------------------------------------------------------------- #
# Reference numbering                                                         #
# --------------------------------------------------------------------------- #

def next_contract_reference(entity):
    """Allocate a sequential, entity-scoped contract reference (e.g. ``COD-CT-2600001``).

    Reuses finance's :func:`~vs_finance.numbering.next_document_number`, which locks the
    per-``(entity, doc_type, fiscal_year)`` :class:`~vs_finance.models.DocumentSequence`
    row with ``select_for_update`` — so two concurrent creates can never be handed the
    same number. Contracts are entity-level master data (no branch), so ``branch=None``.
    The unique-per-entity ``reference`` constraint remains the final safety net.
    """
    from django.utils import timezone
    from vs_finance.numbering import next_document_number

    return next_document_number(
        entity=entity, branch=None, doc_type=CONTRACT_DOC_TYPE,
        fiscal_year=timezone.now().year,
    )


# --------------------------------------------------------------------------- #
# Contract lifecycle                                                          #
# --------------------------------------------------------------------------- #

@transaction.atomic
def activate_contract(contract, *, actor_user=None):
    """Bring a DRAFT contract into force. Requires both a start and an end date."""
    from .models import Vendor, VendorContract

    supplied_contract = contract
    contract = VendorContract.objects.select_for_update(of=("self",)).get(pk=contract.pk)
    if contract.vendor.entity_id != contract.entity_id:
        raise ContractError("The contract vendor must belong to the same entity.")
    # Activation and vendor governance updates serialize on the same master row.
    contract.vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=contract.vendor_id)
    if contract.status != ContractStatus.DRAFT:
        raise ContractError(
            f"Contract {contract.reference} is '{contract.status}'; "
            f"only a draft contract can be activated.",
        )
    if contract.start_date is None or contract.end_date is None:
        raise ContractError("A contract needs a start_date and end_date before activation.")
    if contract.end_date < contract.start_date:
        raise ContractError("A contract's end_date cannot precede its start_date.")
    if reason := vendor_purchase_block_reason(contract.vendor):
        raise ContractError(reason)
    contract.status = ContractStatus.ACTIVE
    contract.save(update_fields=["status", "updated_at"])
    record(
        entity=contract.entity, action=FinanceAuditAction.VENDOR_CONTRACT_ACTIVATED,
        actor_user=actor_user, target=contract,
        message=f"Activated contract {contract.reference} with {contract.vendor.code} "
                f"({contract.start_date} → {contract.end_date}).",
        vendor_id=contract.vendor_id,
    )
    # Preserve the service's historical mutation contract for callers that retain
    # the object they passed in instead of using the return value.
    supplied_contract.status = contract.status
    return supplied_contract


def terminate_contract(contract, *, reason="", actor_user=None):
    """End a contract early. Idempotent on terminal states; refuses on DRAFT."""
    if contract.status in (ContractStatus.TERMINATED, ContractStatus.EXPIRED,
                           ContractStatus.RENEWED):
        return contract
    if contract.status == ContractStatus.DRAFT:
        raise ContractError("A draft contract cannot be terminated; cancel/delete it instead.")
    contract.status = ContractStatus.TERMINATED
    contract.save(update_fields=["status", "updated_at"])
    record(
        entity=contract.entity, action=FinanceAuditAction.VENDOR_CONTRACT_TERMINATED,
        actor_user=actor_user, target=contract,
        message=f"Terminated contract {contract.reference}."
                + (f" Reason: {reason}" if reason else ""),
        vendor_id=contract.vendor_id,
    )
    return contract


@transaction.atomic
def renew_contract(contract, *, reference, start_date, end_date, contract_value=None,
                   copy_milestones=False, actor_user=None):
    """Create a successor contract that replaces ``contract`` and mark the old one RENEWED.

    The new contract starts ACTIVE (carrying the vendor, terms and — unless overridden —
    the same value), points its ``renews`` back at the original, and optionally copies the
    PENDING milestones forward. The original flips to RENEWED.
    """
    from .models import ContractMilestone, Vendor, VendorContract

    contract = VendorContract.objects.select_for_update(of=("self",)).get(pk=contract.pk)
    if contract.vendor.entity_id != contract.entity_id:
        raise ContractError("The contract vendor must belong to the same entity.")
    contract.vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=contract.vendor_id)

    if contract.status not in (ContractStatus.ACTIVE, ContractStatus.EXPIRED):
        raise ContractError(
            f"Contract {contract.reference} is '{contract.status}'; only an active or "
            f"expired contract can be renewed.",
        )
    if end_date < start_date:
        raise ContractError("The renewal's end_date cannot precede its start_date.")
    if reason := vendor_purchase_block_reason(contract.vendor):
        raise ContractError(reason)

    successor = VendorContract.objects.create(
        entity=contract.entity, vendor=contract.vendor, reference=reference,
        title=contract.title, status=ContractStatus.ACTIVE,
        start_date=start_date, end_date=end_date,
        contract_value=contract.contract_value if contract_value is None else contract_value,
        payment_terms=contract.payment_terms,
        auto_renew=contract.auto_renew, renewal_notice_days=contract.renewal_notice_days,
        renews=contract, notes=contract.notes, created_by=actor_user,
    )
    if copy_milestones:
        pending = contract.milestones.filter(status=MilestoneStatus.PENDING).order_by("line_no", "id")
        for ms in pending:
            ContractMilestone.objects.create(
                contract=successor, name=ms.name, due_date=ms.due_date,
                amount=ms.amount, note=ms.note, line_no=ms.line_no,
            )

    contract.status = ContractStatus.RENEWED
    contract.save(update_fields=["status", "updated_at"])
    record(
        entity=contract.entity, action=FinanceAuditAction.VENDOR_CONTRACT_RENEWED,
        actor_user=actor_user, target=successor,
        message=f"Renewed contract {contract.reference} → {successor.reference} "
                f"({start_date} → {end_date}).",
        vendor_id=contract.vendor_id, renews_id=contract.pk,
    )
    return successor


# --------------------------------------------------------------------------- #
# Milestones                                                                  #
# --------------------------------------------------------------------------- #

def complete_milestone(milestone, *, on=None, actor_user=None):
    """Tick off a milestone as COMPLETED (idempotent). Sets ``completed_date``."""
    if milestone.status == MilestoneStatus.COMPLETED:
        return milestone
    milestone.status = MilestoneStatus.COMPLETED
    milestone.completed_date = on or datetime.date.today()
    milestone.save(update_fields=["status", "completed_date", "updated_at"])
    record(
        entity=milestone.contract.entity, action=FinanceAuditAction.CONTRACT_MILESTONE_COMPLETED,
        actor_user=actor_user, target=milestone.contract,
        message=f"Completed milestone '{milestone.name}' on contract "
                f"{milestone.contract.reference}.",
        contract_id=milestone.contract_id, milestone_id=milestone.pk,
    )
    return milestone


def flag_missed_milestones(entity, *, as_of=None):
    """Flip PENDING milestones whose due_date has passed to MISSED. Returns the count."""
    as_of = as_of or datetime.date.today()
    from .models import ContractMilestone

    return ContractMilestone.objects.filter(
        contract__entity=entity, status=MilestoneStatus.PENDING,
        due_date__isnull=False, due_date__lt=as_of,
    ).update(status=MilestoneStatus.MISSED)


# --------------------------------------------------------------------------- #
# Expiry / renewal alerts                                                     #
# --------------------------------------------------------------------------- #

def mark_expired(entity, *, as_of=None):
    """Flip ACTIVE contracts past their end_date to EXPIRED. Returns the count."""
    as_of = as_of or datetime.date.today()
    from .models import VendorContract

    return VendorContract.objects.filter(
        entity=entity, status=ContractStatus.ACTIVE,
        end_date__isnull=False, end_date__lt=as_of,
    ).update(status=ContractStatus.EXPIRED)


def expiring_contracts(entity, *, as_of=None, within_days=None):
    """ACTIVE contracts due for renewal — i.e. inside their renewal-notice window.

    A contract qualifies when ``as_of`` has reached its ``renewal_window_start`` (i.e.
    ``end_date - renewal_notice_days``) and it has not yet lapsed. Pass ``within_days`` to
    override every contract's own notice period with a single horizon
    (``end_date <= as_of + within_days``). Ordered soonest-expiring first.
    """
    as_of = as_of or datetime.date.today()
    from .models import VendorContract

    qs = VendorContract.objects.filter(
        entity=entity, status=ContractStatus.ACTIVE, end_date__isnull=False,
    ).select_related("vendor")

    if within_days is not None:
        horizon = as_of + datetime.timedelta(days=int(within_days))
        qs = qs.filter(end_date__gte=as_of, end_date__lte=horizon)
        return list(qs.order_by("end_date", "reference"))

    # Per-contract notice window: end_date - renewal_notice_days <= as_of <= end_date.
    return [
        c for c in qs.order_by("end_date", "reference")
        if c.end_date >= as_of and c.renewal_window_start() <= as_of
    ]
