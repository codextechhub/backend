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
from __future__ import annotations  # Defer annotation evaluation for forward references.

import datetime  # Used for date and datetime fields on reconciliation rows.
from dataclasses import dataclass, field  # Lightweight containers for report rows.

from vs_finance.models import BankStatementLine  # Imported bank statement rows to reconcile against.
from vs_finance.money import format_naira  # Format signed kobo values as naira strings.

from .constants import CollectionStatus, PayoutStatus  # Gateway lifecycle statuses used for filtering.
from .models import CollectionIntent, PayoutInstruction  # Gateway records to reconcile.


@dataclass  # Apply the decorator to this callable.
class SettlementRow:  # Define the class used by this module.
    """One gateway-confirmed movement and the bank line (if any) it settled to."""

    kind: str                       # "COLLECTION" | "PAYOUT"  # Direction of the gateway movement.
    gateway_id: int  # Primary key of the gateway record.
    reference: str  # Merchant reference used for matching.
    provider: str  # Provider name that produced the record.
    provider_reference: str  # Provider-side transaction reference.
    amount: int                     # signed kobo (+ in, - out)  # Signed gateway amount for matching.
    confirmed_at: datetime.datetime | None  # Confirmation timestamp.
    matched_bank_line_id: int | None = None  # Bank statement line matched to this movement, if any.
    match_basis: str = ""           # "reference" | "amount" | ""  # Which rule found the match.
    settled: bool = False  # Whether a bank line was matched.
    settled_amount: int | None = None    # the matched bank line's signed amount (net of fees)  # Bank-side amount.
    settlement_reference: str = ""       # the matched bank line's reference  # Bank-side reference.
    settlement_date: datetime.date | None = None   # the matched bank line's txn date  # Bank-side transaction date.
    settlement_description: str = ""               # the matched bank line's description  # Bank-side description.

    @property  # Apply the decorator to this callable.
    def amount_naira(self) -> str:  # Define the callable used by this module.
        return format_naira(self.amount)  # Render the signed amount for display.

    @property  # Apply the decorator to this callable.
    def fee_amount(self) -> int:  # Define the callable used by this module.
        """Gross (gateway) minus net (bank) — the PSP fee, in absolute kobo."""
        if self.settled_amount is None:  # No settlement means we cannot derive a fee yet.
            return 0  # Return the computed module result.
        return abs(self.amount) - abs(self.settled_amount)  # Fee is the absolute difference between gross and net.


@dataclass  # Apply the decorator to this callable.
class UnmatchedBankLine:  # Define the class used by this module.
    """A bank statement line with no corresponding gateway record."""

    bank_line_id: int  # Primary key of the unmatched bank line.
    bank_account_id: int  # Owning bank account.
    txn_date: datetime.date  # Bank transaction date.
    description: str  # Bank memo/description.
    reference: str  # Bank reference string.
    amount: int                     # signed kobo  # Signed bank amount.

    @property  # Apply the decorator to this callable.
    def amount_naira(self) -> str:  # Define the callable used by this module.
        return format_naira(self.amount)  # Render the signed amount for display.


@dataclass  # Apply the decorator to this callable.
class SettlementReconciliation:  # Define the class used by this module.
    """The full picture for an entity over a date window."""

    entity_id: int  # Entity being reconciled.
    entity_code: str  # Human-readable entity code.
    start_date: datetime.date | None  # Inclusive start of the reporting window.
    end_date: datetime.date | None  # Inclusive end of the reporting window.
    provider: str  # Optional provider filter.
    rows: list = field(default_factory=list)  # Matched gateway rows.
    unmatched_bank_lines: list = field(default_factory=list)  # Bank lines with no gateway match.

    @property  # Apply the decorator to this callable.
    def settled_count(self) -> int:  # Define the callable used by this module.
        return sum(1 for r in self.rows if r.settled)  # Count rows that matched a bank line.

    @property  # Apply the decorator to this callable.
    def unsettled_count(self) -> int:  # Define the callable used by this module.
        return sum(1 for r in self.rows if not r.settled)  # Count rows still unmatched.

    @property  # Apply the decorator to this callable.
    def gateway_total(self) -> int:  # Define the callable used by this module.
        return sum(r.amount for r in self.rows)  # Total signed gateway movement.

    @property  # Apply the decorator to this callable.
    def settled_total(self) -> int:  # Define the callable used by this module.
        return sum(r.amount for r in self.rows if r.settled)  # Total signed amount already settled.

    @property  # Apply the decorator to this callable.
    def unsettled_total(self) -> int:  # Define the callable used by this module.
        return sum(r.amount for r in self.rows if not r.settled)  # Total signed amount still open.

    @property  # Apply the decorator to this callable.
    def unmatched_bank_total(self) -> int:  # Define the callable used by this module.
        return sum(b.amount for b in self.unmatched_bank_lines)  # Total signed amount on unexplained bank lines.

    @property  # Apply the decorator to this callable.
    def is_reconciled(self) -> bool:  # Define the callable used by this module.
        """True iff every gateway record settled and no bank line is left unexplained."""
        return self.unsettled_count == 0 and not self.unmatched_bank_lines  # Full reconciliation means no open items.


def settlement_reconciliation(entity, *, start_date=None, end_date=None, provider=None):  # Define the callable used by this module.
    """Reconcile gateway-confirmed movements against ``entity``'s imported bank lines.

    ``start_date``/``end_date`` bound both the gateway confirmation date and the bank line
    transaction date (inclusive). ``provider`` optionally narrows to one PSP. Returns a
    :class:`SettlementReconciliation`.
    """
    rows: list[SettlementRow] = []  # Collect gateway movements into reconciliation rows.

    collections = CollectionIntent.objects.filter(  # Start from succeeded collections.
        entity=entity, status=CollectionStatus.SUCCEEDED,  # Continue the structured value.
    )  # Close the grouped expression.
    payouts = PayoutInstruction.objects.filter(  # Start from paid payouts.
        entity=entity, status=PayoutStatus.PAID,  # Continue the structured value.
    )  # Close the grouped expression.
    if provider:  # Optional PSP filter narrows the report to one provider.
        collections = collections.filter(provider=provider)  # Limit collections to the chosen provider.
        payouts = payouts.filter(provider=provider)  # Limit payouts to the chosen provider.

    for ci in collections.only(  # Iterate only over the fields needed for the report.
        "id", "reference", "provider", "provider_reference", "amount", "confirmed_at",
    ):  # Start the nested execution block.
        confirmed = ci.confirmed_at  # Confirmation timestamp for the collection.
        if not _date_in_window(confirmed, start_date, end_date):  # Skip rows outside the window.
            continue  # Skip to the next loop iteration.
        rows.append(SettlementRow(  # Collections are positive signed movements.
            kind="COLLECTION", gateway_id=ci.id, reference=ci.reference,
            provider=ci.provider, provider_reference=ci.provider_reference,  # Continue the structured value.
            amount=int(ci.amount), confirmed_at=confirmed,  # Continue the structured value.
        ))  # Execute the module statement.
    for po in payouts.only(  # Iterate over paid payouts using only the required columns.
        "id", "reference", "provider", "provider_reference", "amount", "confirmed_at",
    ):  # Start the nested execution block.
        confirmed = po.confirmed_at  # Confirmation timestamp for the payout.
        if not _date_in_window(confirmed, start_date, end_date):  # Skip rows outside the window.
            continue  # Skip to the next loop iteration.
        rows.append(SettlementRow(  # Payouts are negative signed movements.
            kind="PAYOUT", gateway_id=po.id, reference=po.reference,
            provider=po.provider, provider_reference=po.provider_reference,  # Continue the structured value.
            amount=-int(po.amount), confirmed_at=confirmed,  # Continue the structured value.
        ))  # Execute the module statement.

    bank_qs = BankStatementLine.objects.filter(bank_account__entity=entity)  # Fetch bank lines for the same entity.
    if start_date is not None:  # Apply the start date filter only when provided.
        bank_qs = bank_qs.filter(txn_date__gte=start_date)  # Inclusive lower bound.
    if end_date is not None:  # Apply the end date filter only when provided.
        bank_qs = bank_qs.filter(txn_date__lte=end_date)  # Inclusive upper bound.
    bank_lines = list(bank_qs.only(  # Materialize the bank lines for matching.
        "id", "bank_account_id", "txn_date", "description", "reference", "amount",
    ))  # Execute the module statement.

    # Index bank lines by reference and by signed amount for two-pass matching.  # Build lookup tables up front.
    by_reference: dict[str, list] = {}  # Reference -> candidate lines.
    by_amount: dict[int, list] = {}  # Signed amount -> candidate lines.
    for line in bank_lines:  # Build both indexes in one pass.
        if line.reference:  # Only reference-bearing lines can participate in the reference pass.
            by_reference.setdefault(line.reference.strip(), []).append(line)  # Group by stripped reference.
        by_amount.setdefault(int(line.amount), []).append(line)  # Group by exact signed amount.

    consumed: set[int] = set()  # Bank line ids already matched.

    def _take(candidates):  # Define the callable used by this module.
        for cand in candidates:  # Walk candidate matches in order.
            if cand.id not in consumed:  # Skip already matched bank lines.
                consumed.add(cand.id)  # Mark the line as used.
                return cand  # Return the first unused candidate.
        return None  # No usable candidate remained.

    # Pass 1: reference match (our reference or the provider's reference).  # Prefer explicit identifiers.
    for row in rows:  # Examine each gateway row once.
        keys = [k for k in (row.reference, row.provider_reference) if k]  # Try our reference first, then PSP reference.
        for key in keys:  # A row may match on either value.
            cand = _take(by_reference.get(key.strip(), []))  # Pull the first unused bank line with that reference.
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
            continue  # Skip to the next loop iteration.
        cand = _take(by_amount.get(row.amount, []))  # Find a bank line with the exact signed amount.
        if cand is not None:  # Amount match found.
            row.matched_bank_line_id = cand.id  # Link the bank line.
            row.match_basis = "amount"  # Record the fallback match basis.
            row.settled = True  # Mark the gateway movement settled.
            row.settled_amount = int(cand.amount)  # Store the bank-side signed amount.
            row.settlement_reference = cand.reference  # Store the bank reference.
            row.settlement_date = cand.txn_date  # Store the bank transaction date.
            row.settlement_description = cand.description  # Store the bank description.

    unmatched = [  # Any bank line not consumed by the two passes remains unexplained.
        UnmatchedBankLine(  # Continue the structured value.
            bank_line_id=line.id, bank_account_id=line.bank_account_id,  # Continue the structured value.
            txn_date=line.txn_date, description=line.description,  # Continue the structured value.
            reference=line.reference, amount=int(line.amount),  # Continue the structured value.
        )  # Close the grouped expression.
        for line in bank_lines if line.id not in consumed  # Preserve only unmatched bank lines.
    ]  # Close the grouped expression.

    return SettlementReconciliation(  # Return the full reconciliation snapshot.
        entity_id=entity.id, entity_code=entity.code,  # Continue the structured value.
        start_date=start_date, end_date=end_date, provider=provider or "",
        rows=rows, unmatched_bank_lines=unmatched,  # Continue the structured value.
    )  # Close the grouped expression.


def _date_in_window(value, start_date, end_date):  # Define the callable used by this module.
    """True if a (datetime) confirmation falls within the inclusive date window."""
    if value is None:  # Missing dates only match when the caller supplied no window.
        return start_date is None and end_date is None  # Return the computed module result.
    day = value.date() if hasattr(value, "date") else value  # Normalize datetimes to plain dates.
    if start_date is not None and day < start_date:  # Respect the lower bound when provided.
        return False  # Return the computed module result.
    if end_date is not None and day > end_date:  # Respect the upper bound when provided.
        return False  # Return the computed module result.
    return True  # The date falls within the inclusive window.
