"""Daily settlement reconciliation: gateway records vs. the bank statement.  # Read-only reconciliation report.

A payment gateway tells us a collection *settled* or a payout was *paid*, but the money
only truly lands when it shows up on the bank statement. This read-side report pairs every
gateway-confirmed movement (``CollectionIntent`` SUCCEEDED → cash in; ``PayoutInstruction``
PAID → cash out) against the imported :class:`vs_finance.BankStatementLine` rows for the
same entity, so operators can see at a glance:  # Compare gateway truth with bank truth.

* which gateway transactions have **not** yet appeared on the bank (unsettled), and
* which bank lines have **no** matching gateway record (unexplained).

Matching is deliberately conservative: first on a shared reference, then — for anything
still open — on an exact signed-amount match within the date window. Money stays integer
**kobo**; the bank line's ``amount`` is signed (+inflow/-outflow) and we sign each gateway
record the same way (collection ``+amount``, payout ``-amount``) so a correct pairing nets
to zero. Nothing here writes — it never mutates a bank line or books a journal.  # Read-only, two-pass matching only.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from vs_finance.models import BankStatementLine
from vs_finance.money import format_naira

from .constants import CollectionStatus, PayoutStatus
from .models import CollectionIntent, PayoutInstruction


@dataclass
# Group behavior for Settlement Row.
class SettlementRow:
    """One gateway-confirmed movement and the bank line (if any) it settled to."""

    kind: str                       # "COLLECTION" | "PAYOUT"  # Direction of the gateway movement.
    gateway_id: int  # Primary key of the gateway record.
    reference: str  # Merchant reference used for matching.
    provider: str  # Provider name that produced the record.
    provider_reference: str  # Provider-side transaction reference.
    amount: int                     # signed kobo (+ in, - out)  # Signed gateway amount for matching.
    confirmed_at: datetime.datetime | None
    matched_bank_line_id: int | None = None  # Bank statement line matched to this movement, if any.
    match_basis: str = ""           # "reference" | "amount" | ""  # Which rule found the match.
    settled: bool = False  # Whether a bank line was matched.
    settled_amount: int | None = None    # the matched bank line's signed amount (net of fees)  # Bank-side amount.
    settlement_reference: str = ""       # the matched bank line's reference  # Bank-side reference.
    settlement_date: datetime.date | None = None
    settlement_description: str = ""               # the matched bank line's description  # Bank-side description.

    @property
    # Handle the amount naira workflow.
    def amount_naira(self) -> str:
        return format_naira(self.amount)  # Render the signed amount for display.

    @property
    # Handle the fee amount workflow.
    def fee_amount(self) -> int:
        """Gross (gateway) minus net (bank) — the PSP fee, in absolute kobo."""
        if self.settled_amount is None:  # No settlement means we cannot derive a fee yet.
            return 0
        return max(0, abs(self.amount) - abs(self.settled_amount))  # Fee is gross − net, clamped so an over-settlement never reads negative.

    @property
    # Flag amount-only matches for a human to confirm.
    def needs_review(self) -> bool:
        """True when this row settled on **amount alone** (no shared reference).

        Reference matching is exact; the amount-only fallback can cross-pair two
        different movements that happen to share a signed amount in the window, so a
        human should confirm the pairing before trusting it.
        """
        return self.settled and self.match_basis == "amount"  # Amount-basis matches are ambiguous.


@dataclass
# Define Unmatched Bank Line values.
class UnmatchedBankLine:
    """A bank statement line with no corresponding gateway record."""

    bank_line_id: int  # Primary key of the unmatched bank line.
    bank_account_id: int  # Owning bank account.
    txn_date: datetime.date
    description: str  # Bank memo/description.
    reference: str  # Bank reference string.
    amount: int                     # signed kobo  # Signed bank amount.

    @property
    # Handle the amount naira workflow.
    def amount_naira(self) -> str:
        return format_naira(self.amount)  # Render the signed amount for display.


@dataclass
# Group behavior for Settlement Reconciliation.
class SettlementReconciliation:
    """The full picture for an entity over a date window."""

    entity_id: int  # Entity being reconciled.
    entity_code: str  # Human-readable entity code.
    start_date: datetime.date | None
    end_date: datetime.date | None
    provider: str  # Optional provider filter.
    rows: list = field(default_factory=list)  # Matched gateway rows.
    unmatched_bank_lines: list = field(default_factory=list)  # Bank lines with no gateway match.

    @property
    # Handle the settled count workflow.
    def settled_count(self) -> int:
        return sum(1 for r in self.rows if r.settled)  # Count rows that matched a bank line.

    @property
    # Handle the unsettled count workflow.
    def unsettled_count(self) -> int:
        return sum(1 for r in self.rows if not r.settled)  # Count rows still unmatched.

    @property
    # Handle the gateway total workflow.
    def gateway_total(self) -> int:
        return sum(r.amount for r in self.rows)  # Total signed gateway movement.

    @property
    # Handle the settled total workflow.
    def settled_total(self) -> int:
        return sum(r.amount for r in self.rows if r.settled)  # Total signed amount already settled.

    @property
    # Handle the unsettled total workflow.
    def unsettled_total(self) -> int:
        return sum(r.amount for r in self.rows if not r.settled)  # Total signed amount still open.

    @property
    # Count amount-only matches a human should confirm.
    def needs_review_count(self) -> int:
        return sum(1 for r in self.rows if r.needs_review)  # Rows matched on amount alone.

    @property
    # Handle the unmatched bank total workflow.
    def unmatched_bank_total(self) -> int:
        return sum(b.amount for b in self.unmatched_bank_lines)  # Total signed amount on unexplained bank lines.

    @property
    # Handle the is reconciled workflow.
    def is_reconciled(self) -> bool:
        """True iff every gateway record settled and no bank line is left unexplained."""
        return self.unsettled_count == 0 and not self.unmatched_bank_lines  # Full reconciliation means no open items.


# Handle the settlement reconciliation workflow.
def settlement_reconciliation(entity, *, start_date=None, end_date=None, provider=None):
    """Reconcile gateway-confirmed movements against ``entity``'s imported bank lines.

    ``start_date``/``end_date`` bound both the gateway confirmation date and the bank line
    transaction date (inclusive). ``provider`` optionally narrows to one PSP. Returns a
    :class:`SettlementReconciliation`.
    """
    rows: list[SettlementRow] = []  # Collect gateway movements into reconciliation rows.

    collections = CollectionIntent.objects.filter(
        entity=entity, status=CollectionStatus.SUCCEEDED,
    )
    payouts = PayoutInstruction.objects.filter(
        entity=entity, status=PayoutStatus.PAID,
    )
    if provider:  # Optional PSP filter narrows the report to one provider.
        collections = collections.filter(provider=provider)
        payouts = payouts.filter(provider=provider)

    for ci in collections.only(  # Iterate only over the fields needed for the report.
        "id", "reference", "provider", "provider_reference", "amount", "confirmed_at",
    ):
        confirmed = ci.confirmed_at  # Confirmation timestamp for the collection.
        if not _date_in_window(confirmed, start_date, end_date):  # Skip rows outside the window.
            continue
        rows.append(SettlementRow(  # Collections are positive signed movements.
            kind="COLLECTION", gateway_id=ci.id, reference=ci.reference,
            provider=ci.provider, provider_reference=ci.provider_reference,
            amount=int(ci.amount), confirmed_at=confirmed,
        ))
    for po in payouts.only(  # Iterate over paid payouts using only the required columns.
        "id", "reference", "provider", "provider_reference", "amount", "confirmed_at",
    ):
        confirmed = po.confirmed_at  # Confirmation timestamp for the payout.
        if not _date_in_window(confirmed, start_date, end_date):  # Skip rows outside the window.
            continue
        rows.append(SettlementRow(  # Payouts are negative signed movements.
            kind="PAYOUT", gateway_id=po.id, reference=po.reference,
            provider=po.provider, provider_reference=po.provider_reference,
            amount=-int(po.amount), confirmed_at=confirmed,
        ))

    bank_qs = BankStatementLine.objects.filter(bank_account__entity=entity)
    if start_date is not None:  # Apply the start date filter only when provided.
        bank_qs = bank_qs.filter(txn_date__gte=start_date)
    if end_date is not None:  # Apply the end date filter only when provided.
        bank_qs = bank_qs.filter(txn_date__lte=end_date)
    bank_lines = list(bank_qs.only(  # Materialize the bank lines for matching.
        "id", "bank_account_id", "txn_date", "description", "reference", "amount",
    ))

    # Index bank lines by reference and by signed amount for two-pass matching.  # Build lookup tables up front.
    by_reference: dict[str, list] = {}  # Reference -> candidate lines.
    by_amount: dict[int, list] = {}  # Signed amount -> candidate lines.
    for line in bank_lines:  # Build both indexes in one pass.
        if line.reference:  # Only reference-bearing lines can participate in the reference pass.
            by_reference.setdefault(line.reference.strip(), []).append(line)  # Group by stripped reference.
        by_amount.setdefault(int(line.amount), []).append(line)  # Group by exact signed amount.

    consumed: set[int] = set()  # Bank line ids already matched.

    # Support the take workflow.
    def _take(candidates):
        for cand in candidates:  # Walk candidate matches in order.
            if cand.id not in consumed:  # Skip already matched bank lines.
                consumed.add(cand.id)  # Mark the line as used.
                return cand  # Return the first unused candidate.
        return None  # No usable candidate remained.

    # Pass 1: reference match (our reference or the provider's reference).  # Prefer explicit identifiers.
    for row in rows:  # Examine each gateway row once.
        keys = [k for k in (row.reference, row.provider_reference) if k]  # Try our reference first, then PSP reference.
        for key in keys:  # A row may match on either value.
            cand = _take(by_reference.get(key.strip(), []))
            if cand is not None:  # Reference match found.
                row.matched_bank_line_id = cand.id  # Link the bank line.
                row.match_basis = "reference"  # Record how the match was made.
                row.settled = True  # Mark the gateway movement settled.
                row.settled_amount = int(cand.amount)  # Store the bank-side signed amount.
                row.settlement_reference = cand.reference  # Store the bank reference.
                row.settlement_date = cand.txn_date  # Store the bank transaction date.
                row.settlement_description = cand.description  # Store the bank description.
                break  # Stop after the first successful reference match.

    # Pass 2: exact signed-amount match for anything still open.  # Fall back to amount matching.
    for row in rows:  # Revisit only the rows still unmatched.
        if row.settled:  # Skip rows already resolved by reference.
            continue
        cand = _take(by_amount.get(row.amount, []))
        if cand is not None:  # Amount match found.
            row.matched_bank_line_id = cand.id  # Link the bank line.
            row.match_basis = "amount"  # Record the fallback match basis.
            row.settled = True  # Mark the gateway movement settled.
            row.settled_amount = int(cand.amount)  # Store the bank-side signed amount.
            row.settlement_reference = cand.reference  # Store the bank reference.
            row.settlement_date = cand.txn_date  # Store the bank transaction date.
            row.settlement_description = cand.description  # Store the bank description.

    unmatched = [  # Any bank line not consumed by the two passes remains unexplained.
        UnmatchedBankLine(
            bank_line_id=line.id, bank_account_id=line.bank_account_id,
            txn_date=line.txn_date, description=line.description,
            reference=line.reference, amount=int(line.amount),
        )
        for line in bank_lines if line.id not in consumed  # Preserve only unmatched bank lines.
    ]

    return SettlementReconciliation(  # Return the full reconciliation snapshot.
        entity_id=entity.id, entity_code=entity.code,
        start_date=start_date, end_date=end_date, provider=provider or "",
        rows=rows, unmatched_bank_lines=unmatched,
    )


# Support the date in window workflow.
def _date_in_window(value, start_date, end_date):
    """True if a (datetime) confirmation falls within the inclusive date window."""
    if value is None:  # Missing dates only match when the caller supplied no window.
        return start_date is None and end_date is None
    day = value.date() if hasattr(value, "date") else value  # Normalize datetimes to plain dates.
    if start_date is not None and day < start_date:  # Respect the lower bound when provided.
        return False
    if end_date is not None and day > end_date:  # Respect the upper bound when provided.
        return False
    return True  # The date falls within the inclusive window.
