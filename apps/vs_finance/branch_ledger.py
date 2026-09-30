"""Ledger balances for one reader: the whole entity, or only their branches' journals.

Every financial statement in :mod:`vs_finance.reports` reads the denormalised
:class:`~vs_finance.models.AccountBalance` aggregates, one row per account per
period. Those rows carry no branch, so on their own they can only answer for the
whole entity, and a bursar posted to one branch who ran the income statement was
shown every branch's revenue.

A journal does carry a branch, taken from the document that raised it. So a
reader's own statements are built from the journal lines of the entries in their
reach, summed per account and period into rows of the same shape. The reach is
the one every transaction is read by
(:func:`vs_rbac.scoping.transaction_branch_scope`): their own branches' journals
only, never one not yet given a branch. Because every journal balances on its own, any set of whole
journals balances too: a branch trial balance still balances, and a branch
balance sheet still satisfies assets = liabilities + equity.

:func:`ledger_balances` is the one place a statement asks for its balances. A
whole-entity reader gets the ``AccountBalance`` queryset exactly as before, so
their figures, and the SQL behind them, do not change. A narrowed reader gets a
:class:`BranchLedger`, which accepts the same ``filter`` calls the statements
make and yields rows with the same attributes.

"Cash" on a branch balance sheet is the cash its own journals moved. Each bank
account belongs to one branch and each document is paid from its own branch's
account, so that is the money in the branch's accounts, except where a journal
still carries no branch (the whole-school reader sees those). The screens label
narrowed statements for that reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Sum

from .constants import DocumentStatus

#: Journal statuses whose lines are in the ledger. A reversed entry stays in, as
#: ``AccountBalance`` keeps it: its reversal is a separate posted entry that nets
#: it off from the reversal date onwards.
LEDGER_STATUSES = (DocumentStatus.POSTED, DocumentStatus.REVERSED)


def ledger_lines(entity=None):
    """The journal lines that are in the ledger: the one rule every money reader uses.

    Reversing a journal marks the original REVERSED and posts a mirror entry with
    debits and credits swapped. Both stay in the ledger, exactly as
    ``AccountBalance`` keeps them, so the pair nets to zero over any window that
    holds both dates. A reader that took POSTED lines only would drop the original
    and keep the mirror, and every void, payroll re-run or corrected document would
    show as a movement with nothing to cancel it.

    Anything that measures money from journal lines (a balance, a movement, a
    statement leg, a register, a reconciliation candidate) starts from this
    queryset rather than filtering on entry status itself. ``entity`` narrows to one
    ledger; without it the caller narrows, typically by account.
    """
    from .models import JournalLine

    lines = JournalLine.objects.filter(entry__status__in=LEDGER_STATUSES)
    if entity is not None:
        lines = lines.filter(entry__entity=entity)
    return lines


@dataclass(frozen=True)
class LedgerRow:
    """One account's movement in one period, shaped like ``AccountBalance``."""

    account: object
    period: object
    debit_total: int
    credit_total: int
    opening_debit: int = 0
    opening_credit: int = 0

    @property
    def account_id(self) -> int:
        return self.account.id

    @property
    def period_id(self) -> int:
        return self.period.id


class BranchLedger:
    """``AccountBalance``-shaped rows over the journals in one reader's reach.

    Supports the lookups the statements use, each translated onto the journal
    line: ``period`` and ``period__…`` read the entry's period, ``account…`` reads
    the line's account. Anything else raises rather than being ignored, so a new
    statement that filters on something this does not understand fails loudly
    instead of silently reporting the whole entity.
    """

    def __init__(self, entity, scope, lookups=None, *, empty=False):
        self.entity = entity
        self.scope = scope
        self.lookups = dict(lookups or {})
        self.empty = empty
        self._rows = None

    def _translate(self, key):
        if key == "period" or key.startswith("period__") or key == "period_id":
            return f"entry__{key}"
        if key.startswith("account"):
            return key
        raise ValueError(f"BranchLedger cannot filter on {key!r}.")

    def filter(self, **lookups):
        """A narrower ledger; lookups combine like successive ``filter`` calls."""
        merged = dict(self.lookups)
        merged.update({self._translate(k): v for k, v in lookups.items()})
        return BranchLedger(self.entity, self.scope, merged, empty=self.empty)

    def select_related(self, *fields):
        """Accounts and periods are always attached; kept for interface parity."""
        return self

    def none(self):
        return BranchLedger(self.entity, self.scope, self.lookups, empty=True)

    def _load(self):
        from .models import Account, FiscalPeriod

        if self.empty:
            return []
        lines = self.scope.filter(ledger_lines(self.entity), "entry__").filter(**self.lookups)
        sums = list(
            lines.values("account_id", "entry__period_id")
            .annotate(dr=Sum("debit"), cr=Sum("credit"))
        )
        accounts = Account.objects.in_bulk({s["account_id"] for s in sums})
        periods = FiscalPeriod.objects.select_related("fiscal_year").in_bulk(
            {s["entry__period_id"] for s in sums})
        return [
            LedgerRow(
                account=accounts[s["account_id"]],
                period=periods[s["entry__period_id"]],
                debit_total=int(s["dr"] or 0),
                credit_total=int(s["cr"] or 0),
            )
            for s in sums
        ]

    def __iter__(self):
        # The statements walk one source several times (once per account type),
        # so the aggregate runs once per ledger and is reused.
        if self._rows is None:
            self._rows = self._load()
        return iter(self._rows)


def ledger_balances(entity, scope=None):
    """The balances a statement for ``entity`` should read, as ``scope`` may see them.

    ``scope`` is a :class:`vs_rbac.scoping.BranchScope`. ``None`` or an unnarrowed
    scope returns the ``AccountBalance`` queryset for the whole entity, unchanged.
    """
    from .models import AccountBalance

    if scope is None or not scope.is_narrowed:
        return AccountBalance.objects.filter(account__entity=entity)
    return BranchLedger(entity, scope)


@dataclass(frozen=True)
class YearBranches:
    """Which branches a fiscal year's ledger lines belong to.

    ``branch_ids`` lists every branch that has an entry in the year, in id order.
    ``unbranched_entries`` counts the entries in the year that carry no branch at
    all; it is ``0`` when every entry has one.
    """

    branch_ids: tuple
    unbranched_entries: int

    @property
    def has_unbranched(self) -> bool:
        return self.unbranched_entries > 0


def branches_in_year(entity, fiscal_year, *, account_types=None) -> YearBranches:
    """The branches whose journals appear in ``fiscal_year``, and how many entries have none.

    Reads the same ledger every money reader uses (:func:`ledger_lines`), so a
    reversed entry and its reversal both count. ``account_types`` narrows to lines
    on accounts of those :class:`~vs_finance.constants.AccountType` values, for a
    caller that cares only about, say, income and expense.

    An entry with no branch is counted rather than dropped. How a caller treats one
    is its own decision: the year-end close files it under the tenant's only
    branch where there is exactly one, and refuses to guess where there are several.
    """
    lines = ledger_lines(entity).filter(entry__period__fiscal_year=fiscal_year)
    if account_types:
        lines = lines.filter(account__account_type__in=list(account_types))
    branch_ids = tuple(sorted(set(
        lines.filter(entry__branch__isnull=False)
        .values_list("entry__branch_id", flat=True).distinct()
    )))
    unbranched = (
        lines.filter(entry__branch__isnull=True)
        .values("entry_id").distinct().count()
    )
    return YearBranches(branch_ids=branch_ids, unbranched_entries=unbranched)
