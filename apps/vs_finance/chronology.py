"""Accounting-date integrity - can this transaction have happened on this date?

The engine has two independent notions of "when":

* **real time** - ``created_at``, the order rows were entered; and
* **the accounting date** - ``invoice_date`` / ``payment_date`` / ``refund_date`` /
  ``note_date`` / ``concession_date`` / ``write_off_date``, the date the movement
  takes effect in the ledger, chosen by the user.

Every sub-ledger guard used to validate against *current, undated* state - an
invoice's ``balance_due`` right now, a customer's lifetime credit right now - and
then post that decision on the user's chosen accounting date. Those two disagree the
moment a date is backdated, which let the books record things that could not have
happened: a refund paying out credit that arrives eight days later, a write-off
clearing an invoice not yet raised, a receipt settling a bill that does not exist.
The symptom is always the same shape - a control account (AR, or customer credit
2140) goes negative for a stretch of the timeline, and no "as at" report can ever be
reconciled.

:func:`ensure_on_or_after` is the one guard those services share. It is deliberately
*not* a period check: :func:`vs_finance.posting.ensure_period_open` answers "may we
book on this date at all?" (a bookkeeping-control question), and this answers "could
this have happened on this date?" (a causality question). A date can easily pass one
and fail the other.

The second half of the module is :func:`credit_lots`, which replaces the old
undated "sum every receipt and credit note, subtract every refund" arithmetic with
dated **lots**: each posted receipt or credit note is a parcel of customer credit
that comes into existence on its own accounting date and is drained, oldest first,
by allocations and refunds. Asking for the lots ``as_of`` a date is what makes
refund availability answerable at the refund's own date rather than at "now".

What backdating *is* still allowed: posting into any open period, receiving cash
before the invoice exists (a genuine prepayment - the cash sits in 2140 and applies
when the invoice arrives), and allocating an older credit to a newer invoice. That
last one is legitimate and common; the rule there is not "refuse" but "date the
reclassification journal on the later of the two documents", which is what
:func:`effective_allocation_date` is for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date

from .constants import CreditNoteKind, DocumentStatus
from .exceptions import BackdatedPostingError


#: The accounting-date field names used across the finance documents, in the order
#: they are probed. Every finance document dates itself with exactly one of these;
#: ``date`` is last so a JournalEntry resolves only after the sub-ledger names fail.
ACCOUNTING_DATE_FIELDS = (
    "invoice_date",
    "payment_date",
    "note_date",
    "refund_date",
    "concession_date",
    "write_off_date",
    "bill_date",
    "date",
)


# Read a finance document's effective accounting date.
def accounting_date(document) -> _date | None:
    """The date ``document`` takes effect in the ledger, or ``None`` if it has none.

    Generic on purpose so shared planners (allocation plans, batch validators) can
    order mixed document types - an invoice against a debit note, a receipt against
    a vendor bill - without every caller learning each model's date field name.
    """
    for field in ACCOUNTING_DATE_FIELDS:  # Probe the known date fields in priority order.
        value = getattr(document, field, None)  # Read the candidate accounting date.
        if value is not None:  # First populated field wins.
            return value
    return None  # Document carries no accounting date.


# Describe a document for a causality error message.
def describe(document, fallback="the source document") -> str:
    """A short human label for ``document`` - its number, else its type and pk."""
    number = getattr(document, "document_number", None)  # Prefer the user-facing number.
    if number:
        return str(number)
    pk = getattr(document, "pk", None)  # Fall back to the model name and pk.
    if pk is not None:
        return f"{type(document).__name__} {pk}"
    return fallback  # Nothing identifying available.


# The single causal-ordering guard every posting service calls.
def ensure_on_or_after(*, subject, subject_date, source, source_date, remedy="") -> None:
    """Raise :class:`BackdatedPostingError` if ``subject_date`` precedes ``source_date``.

    ``subject`` is the dependent movement ("Refund RF-126", "Write-off WO-12") and
    ``source`` is what it draws on ("the credit on RC-128", "invoice INV-9"); both
    are plain strings so the message reads as a sentence. ``remedy`` is appended
    verbatim - give the user the concrete way out ("Date it 9 Sep 2026 or later.").

    A missing date on either side is not an error here: the period guard already
    refuses a dateless posting, and this guard must not turn an unrelated
    misconfiguration into a confusing causality message.
    """
    if subject_date is None or source_date is None:  # Nothing to compare; other guards own this.
        return
    if subject_date < source_date:  # The dependent movement predates the value it consumes.
        raise BackdatedPostingError(
            subject=subject, subject_date=subject_date,
            source=source, source_date=source_date, remedy=remedy,
        )


# Pick the date an allocation between two documents actually takes effect.
def effective_allocation_date(credit_date, target_dates) -> _date:
    """The later of a credit's date and the newest document it is being applied to.

    Applying an older credit to a newer invoice is ordinary business (a prepayment
    finding its bill), so it must not be refused - but the reclassification journal
    ``Dr customer credit · Cr AR`` cannot be dated before the receivable it clears
    exists, or AR carries a credit balance for the gap. Dating the journal at the
    later of the two is the correct and standard treatment.
    """
    dates = [d for d in ([credit_date] + list(target_dates or [])) if d is not None]
    return max(dates) if dates else credit_date  # Latest participating date wins.


# --------------------------------------------------------------------------- #
# Customer credit as dated lots                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
# One parcel of customer credit sitting in the 2140 liability.
class CreditLot:
    """A posted receipt or credit note, and how much of it is still unspent credit.

    ``remaining`` is what is left in the customer-credit liability for this document:
    its value less what has been allocated to invoices and less what has already been
    refunded out. ``date`` is the lot's own accounting date - the date from which its
    credit exists at all, and the whole point of modelling credit as lots.
    """

    kind: str            # "RECEIPT" or "CREDIT_NOTE".
    document_id: int     # Primary key of the receipt / credit note.
    customer_id: int     # Owning customer.
    date: _date          # Accounting date the credit came into existence.
    number: str          # Document number, for messages and audit metadata.
    remaining: int       # Kobo of this lot still sitting in 2140.


# Load a customer's credit as dated, drainable lots.
def credit_lots(entity, customer_ids=None, *, as_of=None) -> dict[int, list[CreditLot]]:
    """Customer credit as FIFO-ordered :class:`CreditLot` parcels, keyed by customer id.

    A lot is included only when its own accounting date is on or before ``as_of``
    (``None`` means "no cutoff - everything posted"). Ordering is oldest-lot-first,
    then by id, so every consumer - the refund payout, the availability endpoint,
    the receipts screen - drains and reports the same parcels in the same order.

    Lots with nothing left are dropped: callers want spendable credit, and keeping
    zero rows would make every FIFO walk scan exhausted receipts forever.
    """
    from .models import CreditNote, Payment

    payments = Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    notes = CreditNote.objects.filter(
        entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.CREDIT)
    if customer_ids is not None:  # Narrow to the customers the caller cares about.
        customer_ids = list(customer_ids)
        payments = payments.filter(customer_id__in=customer_ids)
        notes = notes.filter(customer_id__in=customer_ids)
    if as_of is not None:  # Only credit that exists by the cutoff may be spent.
        payments = payments.filter(payment_date__lte=as_of)
        notes = notes.filter(note_date__lte=as_of)

    lots: dict[int, list[CreditLot]] = {}
    rows = [  # Receipts: cash beyond what it settled is credit.
        CreditLot(
            kind="RECEIPT", document_id=row["id"], customer_id=row["customer_id"],
            date=row["payment_date"], number=row["document_number"] or "",
            remaining=int(row["amount"]) - int(row["allocated_amount"]) - int(row["refunded_amount"]),
        )
        for row in payments.values(
            "id", "customer_id", "payment_date", "document_number",
            "amount", "allocated_amount", "refunded_amount")
    ] + [  # Credit notes: value beyond what it settled is credit.
        CreditLot(
            kind="CREDIT_NOTE", document_id=row["id"], customer_id=row["customer_id"],
            date=row["note_date"], number=row["document_number"] or "",
            remaining=int(row["total"]) - int(row["allocated_amount"]) - int(row["refunded_amount"]),
        )
        for row in notes.values(
            "id", "customer_id", "note_date", "document_number",
            "total", "allocated_amount", "refunded_amount")
    ]
    for lot in rows:  # Bucket the spendable lots per customer.
        if lot.remaining > 0:  # Exhausted lots are not credit.
            lots.setdefault(lot.customer_id, []).append(lot)
    for customer_lots in lots.values():  # FIFO: oldest credit is spent first.
        customer_lots.sort(key=lambda lot: (lot.date, lot.document_id))
    return lots


# Choose which lots a payout draws from, oldest first.
def plan_credit_draw(lots, amount: int) -> list[tuple[CreditLot, int]]:
    """Split ``amount`` kobo across ``lots`` FIFO, returning ``[(lot, taken)]``.

    Stops when the amount is satisfied. Returns a short plan if the lots cannot cover
    it - the caller is expected to have already checked availability and to treat a
    shortfall as a guard failure rather than silently under-refunding.
    """
    plan: list[tuple[CreditLot, int]] = []
    remaining = int(amount)  # Kobo still to be drawn.
    for lot in lots:  # Walk the lots oldest-first.
        if remaining <= 0:  # Requested amount fully covered.
            break
        take = min(lot.remaining, remaining)  # Never take more than the lot holds.
        if take <= 0:  # Skip lots with nothing to give.
            continue
        plan.append((lot, take))  # Record the draw against this lot.
        remaining -= take  # Reduce the outstanding requirement.
    return plan
