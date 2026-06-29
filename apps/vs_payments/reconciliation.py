"""Daily settlement reconciliation: gateway records vs. the bank statement.

A payment gateway tells us a collection *settled* or a payout was *paid*, but the money
only truly lands when it shows up on the bank statement. This read-side report pairs every
gateway-confirmed movement (``CollectionIntent`` SUCCEEDED → cash in; ``PayoutInstruction``
PAID → cash out) against the imported :class:`vs_finance.BankStatementLine` rows for the
same entity, so operators can see at a glance:

* which gateway transactions have **not** yet appeared on the bank (unsettled), and
* which bank lines have **no** matching gateway record (unexplained).

Matching is deliberately conservative: first on a shared reference, then — for anything
still open — on an exact signed-amount match within the date window. Money stays integer
**kobo**; the bank line's ``amount`` is signed (+inflow/-outflow) and we sign each gateway
record the same way (collection ``+amount``, payout ``-amount``) so a correct pairing nets
to zero. Nothing here writes — it never mutates a bank line or books a journal.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from vs_finance.models import BankStatementLine
from vs_finance.money import format_naira

from .constants import CollectionStatus, PayoutStatus
from .models import CollectionIntent, PayoutInstruction


@dataclass
class SettlementRow:
    """One gateway-confirmed movement and the bank line (if any) it settled to."""

    kind: str                       # "COLLECTION" | "PAYOUT"
    gateway_id: int
    reference: str
    provider: str
    provider_reference: str
    amount: int                     # signed kobo (+ in, - out)
    confirmed_at: datetime.datetime | None
    matched_bank_line_id: int | None = None
    match_basis: str = ""           # "reference" | "amount" | ""
    settled: bool = False
    settled_amount: int | None = None    # the matched bank line's signed amount (net of fees)
    settlement_reference: str = ""       # the matched bank line's reference
    settlement_date: datetime.date | None = None   # the matched bank line's txn date
    settlement_description: str = ""               # the matched bank line's description

    @property
    def amount_naira(self) -> str:
        return format_naira(self.amount)

    @property
    def fee_amount(self) -> int:
        """Gross (gateway) minus net (bank) — the PSP fee, in absolute kobo."""
        if self.settled_amount is None:
            return 0
        return abs(self.amount) - abs(self.settled_amount)


@dataclass
class UnmatchedBankLine:
    """A bank statement line with no corresponding gateway record."""

    bank_line_id: int
    bank_account_id: int
    txn_date: datetime.date
    description: str
    reference: str
    amount: int                     # signed kobo

    @property
    def amount_naira(self) -> str:
        return format_naira(self.amount)


@dataclass
class SettlementReconciliation:
    """The full picture for an entity over a date window."""

    entity_id: int
    entity_code: str
    start_date: datetime.date | None
    end_date: datetime.date | None
    provider: str
    rows: list = field(default_factory=list)
    unmatched_bank_lines: list = field(default_factory=list)

    @property
    def settled_count(self) -> int:
        return sum(1 for r in self.rows if r.settled)

    @property
    def unsettled_count(self) -> int:
        return sum(1 for r in self.rows if not r.settled)

    @property
    def gateway_total(self) -> int:
        return sum(r.amount for r in self.rows)

    @property
    def settled_total(self) -> int:
        return sum(r.amount for r in self.rows if r.settled)

    @property
    def unsettled_total(self) -> int:
        return sum(r.amount for r in self.rows if not r.settled)

    @property
    def unmatched_bank_total(self) -> int:
        return sum(b.amount for b in self.unmatched_bank_lines)

    @property
    def is_reconciled(self) -> bool:
        """True iff every gateway record settled and no bank line is left unexplained."""
        return self.unsettled_count == 0 and not self.unmatched_bank_lines


def settlement_reconciliation(entity, *, start_date=None, end_date=None, provider=None):
    """Reconcile gateway-confirmed movements against ``entity``'s imported bank lines.

    ``start_date``/``end_date`` bound both the gateway confirmation date and the bank line
    transaction date (inclusive). ``provider`` optionally narrows to one PSP. Returns a
    :class:`SettlementReconciliation`.
    """
    rows: list[SettlementRow] = []

    collections = CollectionIntent.objects.filter(
        entity=entity, status=CollectionStatus.SUCCEEDED,
    )
    payouts = PayoutInstruction.objects.filter(
        entity=entity, status=PayoutStatus.PAID,
    )
    if provider:
        collections = collections.filter(provider=provider)
        payouts = payouts.filter(provider=provider)

    for ci in collections.only(
        "id", "reference", "provider", "provider_reference", "amount", "confirmed_at",
    ):
        confirmed = ci.confirmed_at
        if not _date_in_window(confirmed, start_date, end_date):
            continue
        rows.append(SettlementRow(
            kind="COLLECTION", gateway_id=ci.id, reference=ci.reference,
            provider=ci.provider, provider_reference=ci.provider_reference,
            amount=int(ci.amount), confirmed_at=confirmed,
        ))
    for po in payouts.only(
        "id", "reference", "provider", "provider_reference", "amount", "confirmed_at",
    ):
        confirmed = po.confirmed_at
        if not _date_in_window(confirmed, start_date, end_date):
            continue
        rows.append(SettlementRow(
            kind="PAYOUT", gateway_id=po.id, reference=po.reference,
            provider=po.provider, provider_reference=po.provider_reference,
            amount=-int(po.amount), confirmed_at=confirmed,
        ))

    bank_qs = BankStatementLine.objects.filter(bank_account__entity=entity)
    if start_date is not None:
        bank_qs = bank_qs.filter(txn_date__gte=start_date)
    if end_date is not None:
        bank_qs = bank_qs.filter(txn_date__lte=end_date)
    bank_lines = list(bank_qs.only(
        "id", "bank_account_id", "txn_date", "description", "reference", "amount",
    ))

    # Index bank lines by reference and by signed amount for two-pass matching.
    by_reference: dict[str, list] = {}
    by_amount: dict[int, list] = {}
    for line in bank_lines:
        if line.reference:
            by_reference.setdefault(line.reference.strip(), []).append(line)
        by_amount.setdefault(int(line.amount), []).append(line)

    consumed: set[int] = set()

    def _take(candidates):
        for cand in candidates:
            if cand.id not in consumed:
                consumed.add(cand.id)
                return cand
        return None

    # Pass 1: reference match (our reference or the provider's reference).
    for row in rows:
        keys = [k for k in (row.reference, row.provider_reference) if k]
        for key in keys:
            cand = _take(by_reference.get(key.strip(), []))
            if cand is not None:
                row.matched_bank_line_id = cand.id
                row.match_basis = "reference"
                row.settled = True
                row.settled_amount = int(cand.amount)
                row.settlement_reference = cand.reference
                row.settlement_date = cand.txn_date
                row.settlement_description = cand.description
                break

    # Pass 2: exact signed-amount match for anything still open.
    for row in rows:
        if row.settled:
            continue
        cand = _take(by_amount.get(row.amount, []))
        if cand is not None:
            row.matched_bank_line_id = cand.id
            row.match_basis = "amount"
            row.settled = True
            row.settled_amount = int(cand.amount)
            row.settlement_reference = cand.reference
            row.settlement_date = cand.txn_date
            row.settlement_description = cand.description

    unmatched = [
        UnmatchedBankLine(
            bank_line_id=line.id, bank_account_id=line.bank_account_id,
            txn_date=line.txn_date, description=line.description,
            reference=line.reference, amount=int(line.amount),
        )
        for line in bank_lines if line.id not in consumed
    ]

    return SettlementReconciliation(
        entity_id=entity.id, entity_code=entity.code,
        start_date=start_date, end_date=end_date, provider=provider or "",
        rows=rows, unmatched_bank_lines=unmatched,
    )


def _date_in_window(value, start_date, end_date):
    """True if a (datetime) confirmation falls within the inclusive date window."""
    if value is None:
        return start_date is None and end_date is None
    day = value.date() if hasattr(value, "date") else value
    if start_date is not None and day < start_date:
        return False
    if end_date is not None and day > end_date:
        return False
    return True
