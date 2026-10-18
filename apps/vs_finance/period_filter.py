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

A period on documents whose own date the caller may not read is refused with
the Field Access refusal (:func:`refuse_hidden_period`), since the rows a year
keeps would give that date away.

Only finance documents are narrowed. A list of anything else (vendors,
contracts, stock) passes through unchanged, because the first date such a row
carries is not the date that places it in a year.
"""
from __future__ import annotations

import datetime
import functools
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

    def contains(self, day: datetime.date) -> bool:
        """Whether ``day`` falls inside the period."""
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)


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
    return period_from_params(_params(request), entity)


def period_from_params(params, entity) -> DocumentPeriod | None:
    """:func:`requested_period` read from a plain mapping of query parameters."""
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


#: A document whose own date is a Field Access field emitted by a serializer
#: built by hand, which names no model the registry could match it through.
_DATE_ACCESS_BY_HAND = {"vs_finance.PayerPayment": ("finance", "payer_payment")}


@functools.lru_cache(maxsize=None)
def date_access_keys(model) -> tuple[tuple[str, str], ...]:
    """``(key, label)`` of each Field Access field that is ``model``'s own date.

    Found through the registry: a resource whose serializer renders ``model``
    and declares the field that places it in a year
    (:func:`vs_finance.retention.document_date_field`) under any of its client
    names. An inter-branch transfer's ``transfer_date`` is the interbranch
    resource's Transaction date, and a provision run's ``as_of`` its Provision
    date. Empty where the date is not a field anyone can hide.
    """
    from django.utils.module_loading import import_string

    from vs_rbac.field_registry import all_declarations

    from .retention import document_date_field

    date_field = document_date_field(model)
    if date_field is None:
        return ()
    by_hand = _DATE_ACCESS_BY_HAND.get(model._meta.label)
    found = {}
    for declaration in all_declarations():
        renders = (declaration.module, declaration.resource) == by_hand or any(
            getattr(getattr(import_string(surface), "Meta", None), "model", None) is model
            for surface in declaration.surfaces
        )
        if not renders:
            continue
        for spec in declaration.fields:
            if date_field in spec.resolved_api_names:
                found[declaration.key_for(spec)] = spec.label
    return tuple(sorted(found.items()))


def refuse_hidden_period(request, model) -> None:
    """Refuse a period on documents whose own date the caller may not read.

    A year picked on a list whose date column is hidden says which documents
    fall in it, which is the date the hidden column exists to keep back: at
    Bright Star, a bursar who cannot see a provision run's date would learn it
    by picking FY2015 and seeing which runs stay. The refusal is the Field
    Access one, naming the field, and is asked before the period is read.
    """
    from vs_rbac.field_enforcement import FieldReadDenied, can_read

    if not asks_for_period(request):
        return
    hidden = [label for key, label in date_access_keys(model) if not can_read(request, key)]
    if hidden:
        params = _params(request)
        named = [name for name in (FISCAL_YEAR_PARAM, DATE_FROM_PARAM, DATE_TO_PARAM)
                 if params.get(name)]
        raise FieldReadDenied(named, labels={name: hidden[0] for name in named})


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
    refuse_hidden_period(request, model)
    date_field = document_date_field(model)
    period = requested_period(request, entity) if date_field else None
    return period.q(date_field, prefix) if period else Q()


def export_period(filter_id: str):
    """The period of a list screen carried into an export, as the dataset's own filters.

    For :attr:`vs_exports.catalogue.Dataset.screen_period`: ``(params, scope)
    -> [spec, ...]``. A named year or range becomes the ``filter_id`` date
    range, the year looked up in the export's own books (``scope.entity``) and
    refused in the list's words, so Mrs Bello exporting Ikeja's FY2015
    invoices gets the file of 1 September 2014 to 31 August 2015 and nothing
    either side of it. ``filter_id`` is the dataset's filter on the date
    :func:`vs_finance.retention.document_date_field` places the document by,
    so the file and the list cut the year on the same day.

    With no period, the file puts away what the list puts away: archived years
    are left out (:func:`vs_finance.archive.export_archive_filter`) unless the
    list shows them (``?include_archived=true``). A named period shows its own
    years, archived or not, as the list does.
    """
    from .archive import HIDE_ARCHIVED_FILTER

    def screen_period(params, scope):
        period = period_from_params(params, getattr(scope, "entity", None))
        if period is None:
            if str(params.get("include_archived", "")).lower() == "true":
                return []
            return [{"id": HIDE_ARCHIVED_FILTER, "value": True}]
        spec = {"id": filter_id}
        if period.start is not None:
            spec["start"] = period.start.isoformat()
        if period.end is not None:
            spec["end"] = period.end.isoformat()
        return [spec]

    return screen_period


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
    refuse_hidden_period(request, queryset.model)
    period = requested_period(request, resolve_entity(request))
    if period is None:
        return hide_archived(queryset, request)
    return queryset.filter(period.q(date_field))
