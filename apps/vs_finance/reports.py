"""Read-side reporting over the ledger.

These functions only *read* the denormalised :class:`~vs_finance.models.AccountBalance`
aggregates that :mod:`vs_finance.posting` maintains, so they are cheap and never
re-sum the whole journal. The cardinal invariant they exist to demonstrate: a
double-entry ledger's debits and credits are always equal, so a trial balance over a
balanced set of postings **always balances**.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Sum

from .money import format_naira


@dataclass
class TrialBalanceRow:
    """One account's debit/credit position on the trial balance (kobo)."""

    account_id: int
    code: str
    name: str
    account_type: str
    debit: int
    credit: int

    @property
    def debit_naira(self) -> str:
        return format_naira(self.debit)

    @property
    def credit_naira(self) -> str:
        return format_naira(self.credit)


@dataclass
class TrialBalance:
    """A trial balance for an entity (optionally a single period).

    ``is_balanced`` is the headline check; in a correct ledger it is always ``True``.
    """

    entity_id: int
    period_id: int | None
    rows: list[TrialBalanceRow] = field(default_factory=list)
    total_debit: int = 0
    total_credit: int = 0

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    @property
    def difference(self) -> int:
        return self.total_debit - self.total_credit


def trial_balance(entity, *, period=None) -> TrialBalance:
    """Build a trial balance for ``entity``, optionally scoped to one ``period``.

    Each account's net position is reduced to a single side: if accumulated debits
    exceed credits the remainder sits in the debit column, else the credit column —
    the conventional trial-balance presentation. Because every posted journal
    balanced, the column totals are equal.
    """
    from .models import AccountBalance

    qs = AccountBalance.objects.filter(account__entity=entity).select_related("account")
    if period is not None:
        qs = qs.filter(period=period)

    # Aggregate across periods (when not period-scoped) per account.
    by_account: dict[int, dict] = {}
    for bal in qs:
        acc = bal.account
        slot = by_account.setdefault(
            acc.id,
            {
                "code": acc.code, "name": acc.name, "account_type": acc.account_type,
                "debit": 0, "credit": 0,
            },
        )
        slot["debit"] += (bal.opening_debit + bal.debit_total)
        slot["credit"] += (bal.opening_credit + bal.credit_total)

    rows: list[TrialBalanceRow] = []
    total_debit = 0
    total_credit = 0
    for account_id, slot in sorted(by_account.items(), key=lambda kv: kv[1]["code"]):
        net = slot["debit"] - slot["credit"]
        debit = net if net > 0 else 0
        credit = -net if net < 0 else 0
        if debit == 0 and credit == 0:
            continue  # net-zero accounts don't clutter the statement
        total_debit += debit
        total_credit += credit
        rows.append(
            TrialBalanceRow(
                account_id=account_id,
                code=slot["code"], name=slot["name"], account_type=slot["account_type"],
                debit=debit, credit=credit,
            )
        )

    return TrialBalance(
        entity_id=entity.id,
        period_id=getattr(period, "id", None),
        rows=rows,
        total_debit=total_debit,
        total_credit=total_credit,
    )
