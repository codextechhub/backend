"""Ledger balances for one reader: the whole entity, or only their branches' journals.

Every financial statement in :mod:`vs_finance.reports` reads the denormalised
:class:`~vs_finance.models.AccountBalance` aggregates, one row per account per
period. Those rows carry no branch, so on their own they can only answer for the
whole entity, and a bursar posted to one branch who ran the income statement was
shown every branch's revenue.

A journal does carry a branch, taken from the document that raised it. So a
reader's own statements are built from the journal lines of the entries in their
reach (their branches plus the school-wide entries, the finance module's
inclusive reading of a blank branch), summed per account and period into rows of
the same shape. Because every journal balances on its own, any set of whole
journals balances too: a branch trial balance still balances, and a branch
balance sheet still satisfies assets = liabilities + equity.

:func:`ledger_balances` is the one place a statement asks for its balances. A
whole-entity reader gets the ``AccountBalance`` queryset exactly as before, so
their figures, and the SQL behind them, do not change. A narrowed reader gets a
:class:`BranchLedger`, which accepts the same ``filter`` calls the statements
make and yields rows with the same attributes.

What a branch statement cannot say is the branch's true bank balance: the school
banks in shared accounts, so "cash" on a branch balance sheet is the cash its own
journals moved, not money it holds separately. The screens label narrowed
statements for that reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Sum

from .constants import DocumentStatus

#: Journal statuses whose lines are in the ledger. A reversed entry stays in, as
#: ``AccountBalance`` keeps it: its reversal is a separate posted entry that nets
#: it off from the reversal date onwards.
LEDGER_STATUSES = (DocumentStatus.POSTED, DocumentStatus.REVERSED)


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
        from .models import Account, FiscalPeriod, JournalLine

        if self.empty:
            return []
        lines = self.scope.filter(
            JournalLine.objects.filter(
                entry__entity=self.entity, entry__status__in=LEDGER_STATUSES,
            ),
            "entry__",
        ).filter(**self.lookups)
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
