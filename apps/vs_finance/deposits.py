"""Refundable customer deposits: held, returned, set against bills, or forfeited.

A deposit line (a fee item of kind ``DEPOSIT``) credits the deposits-held
liability when its invoice posts, and :func:`open_deposits` records it per
customer as a :class:`~vs_finance.models.CustomerDeposit`. It is never revenue.

When the customer leaves (:func:`on_customer_left`, called as the account is
deactivated) the deposit becomes theirs to claim, and the unclaimed-deposit clock
starts. It then leaves the liability in one of three ways:

* **returned**: :func:`release_deposits` raises a credit note against the
  deposits-held account. The part of the deposit its own bill never collected is
  cancelled against that bill first (a deposit never paid is not refunded); the
  rest becomes the customer's credit, which the ordinary refund route pays out.
* **set against unpaid bills**: the same, with the rest applied to the customer's
  other unpaid bills of that branch before any remains as credit. Only where the
  entity's policy allows it (``deposits_offset_unpaid_bills``, off by default),
  and then automatically as the customer leaves owing money.
* **forfeited**: :func:`forfeit_unclaimed_deposits` takes deposits still held a
  set number of years after the customer left to income, one journal per branch.

Mr Bello's son leaves Corona in July 2026 with his N50,000 caution deposit paid
and N30,000 of fees unpaid. With the offset allowed, the deposit clears the
N30,000 and N20,000 is left as credit to refund to him; without it, the N50,000
is returned whole and the N30,000 is chased as before.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

from django.db import transaction

from vs_config.clock import branch_today, tenant_today

from .account_mappings import resolve_mapped_account
from .audit import record, record_rejection
from .constants import (
    AccountMappingKey,
    CreditNoteKind,
    DepositStatus,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from .exceptions import FinanceError, PostingError


def open_deposits(invoice, lines) -> list:
    """Record the deposit each posted deposit line billed. Returns the deposits."""
    from .models import CustomerDeposit

    return CustomerDeposit.objects.bulk_create([
        CustomerDeposit(
            entity_id=invoice.entity_id, branch_id=invoice.branch_id,
            customer_id=invoice.customer_id, invoice=invoice, line=line,
            amount=int(line.net_amount),
        )
        for line in lines if int(line.net_amount) > 0
    ])


def cancel_invoice_deposits(invoice) -> int:
    """Cancel the deposits of an invoice being voided; refuse one already returned.

    A deposit already returned or forfeited has left the liability through its own
    document, so voiding the bill that raised it would take it out a second time.
    """
    from .models import CustomerDeposit

    rows = list(CustomerDeposit.objects.select_for_update().filter(invoice=invoice))
    settled = [row for row in rows if row.status in (DepositStatus.RELEASED, DepositStatus.FORFEITED)]
    if settled:
        raise PostingError(
            f"Invoice {invoice.document_number} billed a deposit that has been "
            f"{settled[0].get_status_display().lower()}; void the credit note that "
            f"released it first.",
        )
    held = [row for row in rows if row.status == DepositStatus.HELD]
    for row in held:
        row.status = DepositStatus.CANCELLED
    CustomerDeposit.objects.bulk_update(held, ["status"])
    return len(held)


def restore_released_deposits(note) -> int:
    """Put back the deposits a voided release note returned. Returns the count."""
    from .models import CustomerDeposit

    return CustomerDeposit.objects.filter(
        release_note=note, status=DepositStatus.RELEASED,
    ).update(status=DepositStatus.HELD, release_note=None)


def _open_bills(customer, branch_id, note_date):
    """The customer's unpaid posted bills of one branch, oldest first, raised by ``note_date``."""
    from vs_rbac.scoping import transaction_branch_match_q

    from .models import Invoice

    return list(
        Invoice.objects.filter(
            transaction_branch_match_q(customer.entity.tenant_id, branch_id),
            customer=customer, status=DocumentStatus.POSTED, invoice_date__lte=note_date,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)
        .order_by("due_date", "invoice_date", "pk")
    )


@transaction.atomic
def release_deposits(customer, *, offset=False, note_date=None, actor_user=None) -> list:
    """Release every deposit ``customer`` holds, one credit note per branch. Returns the notes.

    Each note debits deposits held for the branch's deposits. It settles the part
    of each deposit its own bill has not collected, then, with ``offset``, the
    customer's other unpaid bills of that branch, oldest first; what is left is
    customer credit for the refund route. ``offset`` is refused where the entity's
    policy does not allow deposits to be set against bills.
    """
    from .credit_notes import post_credit_note
    from .models import CreditNote, CreditNoteLine, CustomerDeposit
    from .receivables_policy import resolve_receivables_policy

    entity = customer.entity
    if offset and not resolve_receivables_policy(entity).deposits_offset_unpaid_bills:
        raise PostingError(
            "These books do not allow a deposit to be set against unpaid bills. Return "
            "it as credit, or change the receivables settings.",
        )
    held = list(
        CustomerDeposit.objects.select_for_update()
        .filter(customer=customer, status=DepositStatus.HELD)
        .select_related("invoice").order_by("pk")
    )
    if not held:
        return []
    account = resolve_mapped_account(entity, AccountMappingKey.DEPOSITS_HELD,
                                     label="customer deposits held")
    by_branch: dict = defaultdict(list)
    for row in held:
        by_branch[row.branch_id].append(row)

    notes = []
    for branch_id, rows in by_branch.items():
        day = note_date or branch_today(entity.tenant, branch_id)
        total = sum(int(row.amount) for row in rows)
        # One object per bill, so a bill named twice (its unpaid deposit, then as an
        # unpaid bill) is settled against the balance the first settlement left.
        bills = {row.invoice_id: row.invoice for row in rows}
        deposit_by_invoice: dict = defaultdict(int)
        for row in rows:
            deposit_by_invoice[row.invoice_id] += int(row.amount)
        allocations = [
            (bills[pk], min(amount, bills[pk].balance_due))
            for pk, amount in deposit_by_invoice.items()
            if bills[pk].status == DocumentStatus.POSTED and bills[pk].balance_due > 0
        ]
        if offset:
            for bill in _open_bills(customer, branch_id, day):
                bill = bills.setdefault(bill.pk, bill)
                allocations.append((bill, bill.total))
        note = CreditNote.objects.create(
            entity=entity, customer=customer, branch_id=branch_id,
            kind=CreditNoteKind.CREDIT, note_date=day, created_by=actor_user,
            reason=("Deposit set against unpaid bills" if offset else "Deposit returned"),
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=account, quantity=1, unit_price=total, line_no=1,
            description="Refundable deposit",
        )
        post_credit_note(note, allocations=allocations, actor_user=actor_user)
        note.refresh_from_db()
        for row in rows:
            row.status = DepositStatus.RELEASED
            row.release_note = note
        CustomerDeposit.objects.bulk_update(rows, ["status", "release_note"])
        record(
            entity=entity, action=FinanceAuditAction.DEPOSIT_RELEASED,
            actor_user=actor_user, target=note,
            message=(f"Released {total} kobo of deposits held for {customer.code}; "
                     f"{note.allocated_amount} kobo settled bills and the rest is credit."),
            customer_code=customer.code, amount=total, settled=note.allocated_amount,
            offset=bool(offset), deposits=[row.pk for row in rows],
        )
        notes.append(note)
    return notes


def on_customer_left(customer, *, actor_user=None) -> None:
    """Open the claim on a departing customer's deposits, and offset them where allowed.

    Called as the customer is deactivated. Every held deposit starts its
    unclaimed-deposit clock today. Where the policy sets deposits against unpaid
    bills and the customer leaves owing, the deposits are released against those
    bills at once. A release that cannot post (a closed month, a missing account) is
    recorded as a rejection and leaves the deposits held for a person to release,
    because a customer who has left must stop being billed whatever the deposit
    posting says.
    """
    from .models import CustomerDeposit, Invoice
    from .receivables_policy import resolve_receivables_policy

    today = tenant_today(customer.entity.tenant)
    held = CustomerDeposit.objects.filter(customer=customer, status=DepositStatus.HELD)
    if not held.exists():
        return
    held.filter(claim_opened_on__isnull=True).update(claim_opened_on=today)
    if not resolve_receivables_policy(customer.entity).deposits_offset_unpaid_bills:
        return
    owes = Invoice.objects.filter(
        customer=customer, status=DocumentStatus.POSTED,
    ).exclude(payment_status=InvoicePaymentStatus.PAID).exists()
    if not owes:
        return
    try:
        with transaction.atomic():
            release_deposits(customer, offset=True, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=customer.entity, action=FinanceAuditAction.DEPOSIT_RELEASED,
            exc=exc, actor_user=actor_user, target=customer,
        )


def on_customer_returned(customer) -> None:
    """Stop the unclaimed-deposit clock of a customer who has come back."""
    from .models import CustomerDeposit

    CustomerDeposit.objects.filter(
        customer=customer, status=DepositStatus.HELD,
    ).update(claim_opened_on=None)


def _years_before(day: datetime.date, years: int) -> datetime.date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February in a year that has none.
        return day.replace(year=day.year - years, day=28)


@transaction.atomic
def forfeit_unclaimed_deposits(entity, *, as_of=None, actor_user=None) -> dict:
    """Take deposits unclaimed past the policy's limit to income, one journal per branch.

    A deposit qualifies when its customer has left, has not come back, and left at
    least ``unclaimed_deposit_years`` before ``as_of``. A deposit whose own bill
    still owes money is skipped, because what was never collected cannot become
    income; it is listed so the bill can be dealt with. Idempotent: a forfeited
    deposit is never taken again. Returns ``{"forfeitures": [...], "skipped": [...]}``.
    """
    from .models import CustomerDeposit, DepositForfeiture, JournalEntry, JournalLine
    from .posting import post_journal, resolve_period
    from .receivables_policy import resolve_receivables_policy

    as_of = as_of or tenant_today(entity.tenant)
    years = resolve_receivables_policy(entity).unclaimed_deposit_years
    cutoff = _years_before(as_of, years)
    rows = list(
        CustomerDeposit.objects.select_for_update(of=("self",))
        .filter(entity=entity, status=DepositStatus.HELD, claim_opened_on__lte=cutoff,
                customer__is_active=False)
        .select_related("invoice", "customer").order_by("branch_id", "pk")
    )
    skipped, by_branch = [], defaultdict(list)
    for row in rows:
        if row.invoice.balance_due > 0:
            skipped.append({"deposit": row.pk, "customer": row.customer.code,
                            "invoice": row.invoice.document_number,
                            "reason": "Its bill is not fully paid."})
            continue
        by_branch[row.branch_id].append(row)

    held = resolve_mapped_account(entity, AccountMappingKey.DEPOSITS_HELD,
                                  label="customer deposits held")
    income = resolve_mapped_account(entity, AccountMappingKey.FORFEITED_DEPOSIT_INCOME,
                                    label="forfeited deposits")
    forfeitures = []
    for branch_id, group in by_branch.items():
        total = sum(int(row.amount) for row in group)
        journal = JournalEntry.objects.create(
            entity=entity, branch_id=branch_id, date=as_of,
            period=resolve_period(entity, as_of), source=JournalSource.SALES,
            narration=f"Deposits unclaimed {years} years after the customer left",
            created_by=actor_user,
        )
        JournalLine.objects.create(entry=journal, account=held, debit=total, credit=0,
                                   description="Deposits held", line_no=1)
        JournalLine.objects.create(entry=journal, account=income, debit=0, credit=total,
                                   description="Forfeited deposits", line_no=2)
        post_journal(journal, actor_user=actor_user)
        forfeiture = DepositForfeiture.objects.create(
            entity=entity, branch_id=branch_id, journal=journal, amount=total,
            created_by=actor_user,
        )
        for row in group:
            row.status = DepositStatus.FORFEITED
            row.forfeiture = forfeiture
        CustomerDeposit.objects.bulk_update(group, ["status", "forfeiture"])
        record(
            entity=entity, action=FinanceAuditAction.DEPOSITS_FORFEITED,
            actor_user=actor_user, target=forfeiture,
            message=f"Took {total} kobo of unclaimed deposits to income.",
            journal_id=journal.pk, amount=total, branch_id=branch_id, years=years,
            deposits=[{"deposit": row.pk, "customer": row.customer.code,
                       "amount": int(row.amount)} for row in group],
        )
        forfeitures.append(forfeiture)
    return {"forfeitures": forfeitures, "skipped": skipped}
