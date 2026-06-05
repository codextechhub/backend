"""Posting-layer guards.

The principle locked in Phase 0: **business rules are enforced where journals are
posted, not in the UI.** A request that somehow bypasses the screens (an API call, a
script, a webhook) must still be unable to post into a closed period or write an
unbalanced entry.

In Phase 0 the ``FiscalPeriod`` model does not exist yet, so these guards are
duck-typed: they operate on any object exposing a ``status`` (and optionally a
``label``). Phase 1 introduces the real model and the full ``post_journal`` service,
which will call these same guards — keeping the enforcement point singular.
"""
from __future__ import annotations

from typing import Iterable, Protocol

from .constants import PERIOD_POSTING_BLOCKED, PERIOD_POSTING_RESTRICTED, PeriodStatus
from .exceptions import PeriodClosedError, UnbalancedJournalError


class _PeriodLike(Protocol):
    status: str

    def __str__(self) -> str:  # pragma: no cover - structural typing only
        ...


def ensure_period_open(period: _PeriodLike, *, allow_restricted: bool = False) -> None:
    """Raise :class:`PeriodClosedError` if ``period`` cannot accept a posting.

    Args:
        period: Any object with a ``status`` string drawn from
            :class:`~vs_finance.constants.PeriodStatus`.
        allow_restricted: When ``True``, soft-closed periods are permitted — used by
            privileged close-process auto-postings (depreciation, accruals). Ordinary
            postings pass ``False`` and are blocked from soft-closed periods too.

    A missing period (``None``) is treated as a hard error: nothing posts without a
    period.
    """
    if period is None:
        raise PeriodClosedError(period_label="<none>", status="missing")

    status = getattr(period, "status", None)
    label = str(period)

    if status in PERIOD_POSTING_BLOCKED:
        raise PeriodClosedError(period_label=label, status=str(status))

    if status in PERIOD_POSTING_RESTRICTED and not allow_restricted:
        raise PeriodClosedError(period_label=label, status=str(status))

    if status != PeriodStatus.OPEN and status not in PERIOD_POSTING_RESTRICTED:
        # Unknown / unset status — fail closed rather than guess.
        raise PeriodClosedError(period_label=label, status=str(status or "unknown"))


def ensure_balanced(debit_kobo: int, credit_kobo: int) -> None:
    """Raise :class:`UnbalancedJournalError` unless debits exactly equal credits.

    Amounts are integer kobo, so equality is exact — there is no rounding tolerance,
    and none is wanted: a ledger that is off by one kobo is wrong.
    """
    if debit_kobo != credit_kobo:
        raise UnbalancedJournalError(debit=debit_kobo, credit=credit_kobo)


def sum_sides(lines: Iterable) -> tuple[int, int]:
    """Sum ``(total_debit, total_credit)`` in kobo over an iterable of journal lines.

    Each line is expected to expose integer ``debit`` and ``credit`` attributes
    (kobo). Lines are one-sided by convention (a line is a debit OR a credit), but
    this tolerates both being present and simply sums them.
    """
    total_debit = 0
    total_credit = 0
    for line in lines:
        total_debit += getattr(line, "debit", 0) or 0
        total_credit += getattr(line, "credit", 0) or 0
    return total_debit, total_credit
