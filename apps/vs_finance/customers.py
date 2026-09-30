"""The customer master record: who may change it, and how it leaves and returns.

A customer is the sub-ledger account behind every invoice, receipt and note raised
against it, so three of its fields hold up the books rather than describe the payer:

* the **source reference** (``source_type`` / ``source_id``), which ties the account
  to the record in the product that opened it. Re-pointed after money has moved, it
  shows one family's debt on another family's screens;
* the **receivable account**, which every posting for the customer debits or
  credits. Changed while documents are open, the AR control and the sub-ledger stop
  agreeing; changed to a bank or revenue account, a bill posts as income backed by
  cash that never arrived;
* the **opening balance**, posted once as an opening invoice when the customer is
  created, and corrected afterwards only by a credit or debit note.

Once a customer has posted activity those are fixed here, and every change to a
customer is written to the finance audit log with its before and after values.

**Deactivating** a customer (one who has left) takes them out of every fee run and
every "bill all active" selection while their debt, documents and place in debtor
lists stay exactly as they were. It is reversible.
"""
from __future__ import annotations

from django.db import transaction

from .audit import record
from .constants import DocumentStatus, FinanceAuditAction
from .exceptions import PostingError

#: Customer fields that cannot change once the customer has posted activity.
FIXED_AFTER_ACTIVITY = ("source_type", "source_id", "receivable_account_id", "opening_balance")


def customer_has_activity(customer) -> bool:
    """Whether any posted or voided document names this customer.

    A voided document counts: its journal and its reversal both still sit in the
    ledger against the customer's receivable account.
    """
    from .models import Concession, CreditNote, CustomerCreditTransfer, Invoice, Payment, Refund

    moved = (DocumentStatus.POSTED, DocumentStatus.REVERSED, DocumentStatus.PENDING_APPROVAL,
             DocumentStatus.APPROVED)
    return any(
        model.objects.filter(customer=customer, status__in=moved).exists()
        for model in (Invoice, Payment, CreditNote, Refund, Concession)
    ) or CustomerCreditTransfer.objects.filter(
        to_customer=customer, status__in=moved,
    ).exists() or CustomerCreditTransfer.objects.filter(
        from_customer=customer, status__in=moved,
    ).exists()


def require_unique_source(customer):
    """Refuse a source reference another account in the same books already holds."""
    from .models import Customer

    if not customer.source_id:
        return
    clash = (
        Customer.objects.filter(
            entity_id=customer.entity_id, source_type=customer.source_type,
            source_id=customer.source_id,
        ).exclude(pk=customer.pk).values_list("code", flat=True).first()
    )
    if clash:
        raise PostingError(
            f"Customer {clash} already holds that source record; one record has one "
            f"account in these books.",
        )


@transaction.atomic
def update_customer(customer, changes: dict, *, actor_user=None):
    """Apply ``changes`` (field -> new value) to ``customer``, audited, under a row lock.

    Refuses a change to a field in :data:`FIXED_AFTER_ACTIVITY` once the customer has
    posted activity, a source reference another account holds, and a receivable
    account that is not an asset of the customer's books. ``is_active`` goes through
    :func:`set_customer_active` so leaving and returning are audited as such.
    Returns the customer.
    """
    from .accounts import require_account_kind
    from .models import Customer

    locked = Customer.objects.select_for_update(of=("self",)).get(pk=customer.pk)
    before = {field: getattr(locked, field) for field in changes}
    moved = [
        field for field in FIXED_AFTER_ACTIVITY
        if field in changes and changes[field] != getattr(locked, field)
    ]
    if moved and customer_has_activity(locked):
        names = ", ".join(field.removesuffix("_id").replace("_", " ") for field in moved)
        raise PostingError(
            f"Customer {locked.code} has posted documents, so its {names} can no longer "
            f"change. Correct a balance with a credit or debit note; open a new customer "
            f"for a different record.",
        )
    for field, value in changes.items():
        if field == "is_active":
            continue
        setattr(locked, field, value)
    if "receivable_account_id" in changes and locked.receivable_account_id is not None:
        require_account_kind(locked.receivable_account, "receivable", entity=locked.entity)
    require_unique_source(locked)
    locked.save()
    if "is_active" in changes:
        set_customer_active(locked, bool(changes["is_active"]), actor_user=actor_user)
    after = {field: getattr(locked, field) for field in changes}
    changed = {field for field in changes if before[field] != after[field] and field != "is_active"}
    if changed:
        record(
            entity=locked.entity, action=FinanceAuditAction.CUSTOMER_UPDATED,
            actor_user=actor_user, target=locked,
            message=f"Updated customer {locked.code}: {', '.join(sorted(changed))}.",
            before={f: _plain(before[f]) for f in sorted(changed)},
            after={f: _plain(after[f]) for f in sorted(changed)},
        )
    customer.refresh_from_db()
    return customer


def _plain(value):
    return value if isinstance(value, (int, str, bool, type(None))) else str(value)


@transaction.atomic
def set_customer_active(customer, active: bool, *, actor_user=None, reason: str = ""):
    """Deactivate (``active=False``) or reactivate a customer, audited. Idempotent.

    Deactivating removes the customer from fee runs and from "bill all active"; it
    leaves every document, balance and debtor-list entry untouched, so a family that
    leaves owing N600,000 is still chased for it and still shown owing it.
    """
    from .models import Customer

    locked = Customer.objects.select_for_update(of=("self",)).get(pk=customer.pk)
    if locked.is_active == bool(active):
        customer.is_active = locked.is_active
        return customer
    locked.is_active = bool(active)
    locked.save(update_fields=["is_active", "updated_at"])
    record(
        entity=locked.entity,
        action=(FinanceAuditAction.CUSTOMER_REACTIVATED if active
                else FinanceAuditAction.CUSTOMER_DEACTIVATED),
        actor_user=actor_user, target=locked,
        message=(f"Reactivated customer {locked.code}." if active
                 else f"Deactivated customer {locked.code}; it is billed no more."),
        reason=reason or "",
    )
    customer.is_active = locked.is_active
    return customer
