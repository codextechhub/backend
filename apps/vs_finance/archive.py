"""Putting old fiscal years away, without ever deleting them.

After a few years a tenant's lists, pickers and dashboard fill with years
nobody works in. Archiving a year takes it out of the default lists of fiscal
years and periods, and the dated document lists that offer the filter, while
keeping every record of it: an archived year is read-only (it is closed or
locked, so nothing posts into it), fully readable, reportable and exportable,
and shown again wherever a caller asks with ``?include_archived=true``.

Archiving is a deliberate act, never automatic. It needs:

* a year that is CLOSED or LOCKED, so nothing in it is still being worked;
* a year that ended at least ``finance.archive.min_age_years`` years ago (a
  per-tenant setting, default two), so last year's comparatives stay at hand;
* a caller reaching the whole tenant and holding ``finance.fiscalyear.archive``
  (enforced by the view).

Unarchiving needs only the key and the reach, and both acts are written to the
finance audit trail with the actor and the reason. An archived year cannot be
reopened until it is unarchived, so a correction to an archived year is always
two recorded decisions.

Example. Bright Star School closes FY2027 in March 2028. With the default of
two years it can be archived from 31 December 2029, two years after it ended;
the bursar archives it in February 2030 and it leaves the year picker. In 2033 the tax authority reviews 2027: the
auditor ticks "Show archived years", picks FY2027 and runs every report.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone

from .audit import record
from .close import require_reason
from .constants import FinanceAuditAction, PeriodStatus
from .exceptions import PeriodCloseError
from .wording import counted, state_word

#: The setting holding the minimum age, in whole years after a year's end.
MIN_AGE_KEY = "finance.archive.min_age_years"
DEFAULT_MIN_AGE_YEARS = 2


def min_age_years(tenant) -> int:
    """How many years after its end a fiscal year may be archived, never below one."""
    from vs_config.conf import get_config

    try:
        years = int(get_config(MIN_AGE_KEY, DEFAULT_MIN_AGE_YEARS, tenant=tenant))
    except (TypeError, ValueError):
        years = DEFAULT_MIN_AGE_YEARS
    return max(1, years)


def earliest_archive_date(fiscal_year) -> datetime.date:
    """The first day ``fiscal_year`` may be archived."""
    end = fiscal_year.end_date
    years = min_age_years(fiscal_year.entity.tenant)
    try:
        return end.replace(year=end.year + years)
    except ValueError:  # 29 February.
        return end.replace(year=end.year + years, day=28)


def archived_ranges(entity) -> list[tuple[datetime.date, datetime.date]]:
    """The ``(start, end)`` dates of every archived year of ``entity``."""
    from .models import FiscalYear

    return list(
        FiscalYear.objects.filter(entity=entity, archived_at__isnull=False)
        .values_list("start_date", "end_date")
    )


def exclude_archived(queryset, entity, date_field: str):
    """``queryset`` without rows whose ``date_field`` falls in an archived year."""
    from django.db.models import Q

    ranges = archived_ranges(entity)
    if not ranges:
        return queryset
    inside = Q()
    for start, end in ranges:
        inside |= Q(**{f"{date_field}__gte": start, f"{date_field}__lte": end})
    return queryset.exclude(inside)


def include_archived(request) -> bool:
    """Whether the caller asked to see archived years (``?include_archived=true``)."""
    params = getattr(request, "query_params", None) or getattr(request, "GET", {})
    return str(params.get("include_archived", "")).lower() == "true"


def hide_archived(queryset, request):
    """Leave documents dated in an archived year out of a default list.

    The one place every finance and procurement document list asks, through
    their shared pagination and list bases. A queryset of anything but a
    finance document passes through unchanged, as does every list where the
    caller asked with ``?include_archived=true``.

    A document still owed (a posted bill whose payment status is unpaid or part
    paid) stays visible whatever its year: archiving puts finished years away,
    and a customer's 2027 bill, still unpaid in 2030, is not finished.
    """
    from django.db.models import Exists, OuterRef, Q, QuerySet

    from .constants import DocumentStatus, InvoicePaymentStatus
    from .models import FiscalYear
    from .models.core import FinanceDocument
    from .retention import document_date_field

    if not isinstance(queryset, QuerySet) or include_archived(request):
        return queryset
    if queryset.query.combinator or queryset.query.is_sliced:  # Cannot be filtered further.
        return queryset
    model = queryset.model
    if not issubclass(model, FinanceDocument):
        return queryset
    date_field = document_date_field(model)
    if date_field is None:
        return queryset
    archived = FiscalYear.objects.filter(
        entity=OuterRef("entity"), archived_at__isnull=False,
        start_date__lte=OuterRef(date_field), end_date__gte=OuterRef(date_field),
    )
    keep = Q()
    if any(f.name == "payment_status" for f in model._meta.concrete_fields):
        keep = Q(
            status=DocumentStatus.POSTED,
            payment_status__in=(InvoicePaymentStatus.UNPAID, InvoicePaymentStatus.PARTIAL),
        )
    hidden = Exists(archived) & ~keep if keep else Exists(archived)
    return queryset.exclude(hidden)


@transaction.atomic
def archive_fiscal_year(entity, fiscal_year, *, actor_user=None, reason=None):
    """Archive a closed ``fiscal_year`` old enough to put away. Audited."""
    from vs_config.clock import tenant_today
    from vs_config.display import format_date

    from .close import _lock_fiscal_year

    reason = require_reason(reason, act=f"archive FY{fiscal_year.year}")
    _lock_fiscal_year(fiscal_year)
    if fiscal_year.archived_at is not None:
        raise PeriodCloseError(f"Fiscal year {fiscal_year.year} is already archived.")
    if fiscal_year.status not in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} is {state_word(fiscal_year)}. Only a closed "
            f"or locked year can be archived.")
    earliest = earliest_archive_date(fiscal_year)
    if tenant_today(entity.tenant) < earliest:
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} can be archived from "
            f"{format_date(earliest, entity.tenant)}, "
            f"{counted(min_age_years(entity.tenant), 'year')} after it ended.",
            earliest_archive_date=earliest.isoformat(),
        )
    fiscal_year.archived_at = timezone.now()
    fiscal_year.archived_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
    fiscal_year.save(update_fields=["archived_at", "archived_by", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_ARCHIVED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=f"Archived FY{fiscal_year.year}.", fiscal_year=fiscal_year.year,
        reason=reason,
    )
    return fiscal_year


@transaction.atomic
def unarchive_fiscal_year(entity, fiscal_year, *, actor_user=None, reason=None):
    """Bring an archived ``fiscal_year`` back into the default lists. Audited."""
    from .close import _lock_fiscal_year

    reason = require_reason(reason, act=f"unarchive FY{fiscal_year.year}")
    _lock_fiscal_year(fiscal_year)
    if fiscal_year.archived_at is None:
        raise PeriodCloseError(f"Fiscal year {fiscal_year.year} is not archived.")
    fiscal_year.archived_at = None
    fiscal_year.archived_by = None
    fiscal_year.save(update_fields=["archived_at", "archived_by", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_UNARCHIVED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=f"Unarchived FY{fiscal_year.year}.", fiscal_year=fiscal_year.year,
        reason=reason,
    )
    return fiscal_year
