"""The billing period an entity is in: a window its owner bills by.

Finance knows months, quarters and fiscal years; it does not know what a school
term or a hospital's billing cycle is, and must not, because the same engine
serves both. Some owners bill by such a period all the same, and their readers
want to ask "how much of this term's fees are paid?", which no calendar window
answers: a parent who pays First Term fees a week before the term starts has
paid this term's fees, and one who clears last term's arrears in week two has
not.

So the owner's own layer names the period and says which invoices belong to it.
A provider is a callable ``provider(entity, as_of) -> BillingPeriod | None``,
named by dotted path in the ``FINANCE_BILLING_PERIOD_PROVIDER`` setting. The
school layer registers one that answers with the current academic term and the
invoices raised from the fee structures linked to it. With no provider, or
none for these books, :func:`current_billing_period` answers ``None`` and a
screen offers calendar windows only.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings
from django.db.models import Q
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class BillingPeriod:
    """One billing period, and the invoices that belong to it.

    ``invoices`` is a filter over :class:`~vs_finance.models.Invoice`; receipts
    belong to the period through the invoices they settled, so an unallocated
    receipt belongs to no period. ``label`` is what a switch calls the window
    ("This term"); ``name`` is the period itself ("First Term 2026/2027").
    """

    key: str
    label: str
    name: str
    start: datetime.date
    end: datetime.date
    invoices: Q


@lru_cache(maxsize=1)
def _provider(path: str):
    return import_string(path)


def current_billing_period(entity, as_of: datetime.date) -> BillingPeriod | None:
    """The period ``entity``'s owner bills by on ``as_of``, or ``None``."""
    path = getattr(settings, "FINANCE_BILLING_PERIOD_PROVIDER", "")
    if not path:
        return None
    return _provider(path)(entity, as_of)
