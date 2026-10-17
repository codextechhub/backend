"""Narrowing a document list to a stretch of time: one fiscal year, or a date range.

A list of documents asks with either

* ``?fiscal_year=<year>``: one of the entity's fiscal years by its label
  (``2015``), the way every finance read names a year; or
* ``?date_from=<YYYY-MM-DD>`` and/or ``?date_to=<YYYY-MM-DD>``: an inclusive
  range, either end left open.

Each document is placed by its own date (:func:`vs_finance.retention.document_date_field`):
an order by its order date, a supplier bill by its invoice date, a payment by its
payment date. A fiscal year is the entity's own year, so where Bright Star School
runs September to August, ``fiscal_year=2015`` covers whatever dates that year
was given, not the calendar year.

The period is ANDed with every other filter, the branch one included: Mrs Bello
asking for Ikeja and 2015 sees Ikeja's 2015 orders, and only those.

A period the caller names replaces the default that leaves archived years out
(:func:`vs_finance.archive.hide_archived`). Asking for 2015 is asking to see
2015, and an archived 2015 would otherwise answer with an empty list that reads
as "nothing happened that year". The period itself bounds the list, so nothing
outside it comes back.

Only finance documents are narrowed. A list of anything else (vendors,
contracts, stock) passes through unchanged, because the first date such a row
carries is not the date that places it in a year.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db.models import Q
from rest_framework.exceptions import ValidationError

#: The request parameters a period is read from.
FISCAL_YEAR_PARAM = "fiscal_year"
DATE_FROM_PARAM = "date_from"
DATE_TO_PARAM = "date_to"


@dataclass(frozen=True)
class DocumentPeriod:
    """An inclusive stretch of dates; either end may be open."""

    start: datetime.date | None
    end: datetime.date | None

    def q(self, date_field: str, prefix: str = "") -> Q:
        """The lookup keeping rows whose ``date_field`` falls inside the period."""
        lookups = {}
        if self.start is not None:
            lookups[f"{prefix}{date_field}__gte"] = self.start
        if self.end is not None:
            lookups[f"{prefix}{date_field}__lte"] = self.end
        return Q(**lookups)


def _params(request):
    return getattr(request, "query_params", None) or getattr(request, "GET", {})


def _iso_date(value, field: str) -> datetime.date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Give the date as YYYY-MM-DD."})


def requested_period(request, entity) -> DocumentPeriod | None:
    """The period the caller asked for, or ``None`` when they asked for none.

    A year the entity does not have is refused rather than answered with an
    empty list, so a typo reads as a typo. The lookup is confined to
    ``entity``, which the caller has already been authorised for, so the
    parameter cannot be used to learn about another entity's years.
    """
    params = _params(request)
    year_ref = str(params.get(FISCAL_YEAR_PARAM) or "").strip()
    start = _iso_date(params.get(DATE_FROM_PARAM), DATE_FROM_PARAM)
    end = _iso_date(params.get(DATE_TO_PARAM), DATE_TO_PARAM)
    if year_ref and (start or end):
        raise ValidationError({
            FISCAL_YEAR_PARAM: "Choose a financial year or a date range, not both.",
        })
    if year_ref:
        from .models import FiscalYear

        fiscal_year = (
            FiscalYear.objects.filter(entity=entity, year=int(year_ref)).first()
            if year_ref.isdigit() else None
        )
        if fiscal_year is None:
            raise ValidationError({FISCAL_YEAR_PARAM: f"There is no financial year {year_ref} in these books."})
        return DocumentPeriod(fiscal_year.start_date, fiscal_year.end_date)
    if start and end and start > end:
        raise ValidationError({DATE_TO_PARAM: "The end date must be on or after the start date."})
    if start or end:
        return DocumentPeriod(start, end)
    return None


def asks_for_period(request) -> bool:
    """Whether the request names a period at all, without resolving it."""
    params = _params(request)
    return any(params.get(name) for name in (FISCAL_YEAR_PARAM, DATE_FROM_PARAM, DATE_TO_PARAM))


def period_q(request, entity, model, prefix: str = "") -> Q:
    """The caller's period as a ``Q`` over ``model``'s own date, or an empty ``Q``.

    For a summary that aggregates the same documents a list returns, so its
    figures count exactly the rows underneath them.
    """
    from .models.core import FinanceDocument
    from .retention import document_date_field

    if not issubclass(model, FinanceDocument):
        return Q()
    date_field = document_date_field(model)
    period = requested_period(request, entity) if date_field else None
    return period.q(date_field, prefix) if period else Q()


def narrow_document_list(queryset, request):
    """A document list narrowed to the caller's period, archived years handled.

    With a period, the list is cut to it and archived years are not hidden (see
    the module docstring). Without one, it is the default list: archived years
    hidden unless ``?include_archived=true``. Lists call this in place of
    :func:`vs_finance.archive.hide_archived`.
    """
    from django.db.models import QuerySet

    from .archive import hide_archived
    from .models.core import FinanceDocument
    from .retention import document_date_field
    from .views import resolve_entity

    if (
        not isinstance(queryset, QuerySet)
        or not issubclass(queryset.model, FinanceDocument)
        or not asks_for_period(request)
    ):
        return hide_archived(queryset, request)
    date_field = document_date_field(queryset.model)
    if date_field is None:
        return hide_archived(queryset, request)
    period = requested_period(request, resolve_entity(request))
    if period is None:
        return hide_archived(queryset, request)
    return queryset.filter(period.q(date_field))
