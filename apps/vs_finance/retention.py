"""How long a tenant's books are kept, and which rows that covers.

**The period.** A record is kept for a number of years counted from the end of
the fiscal year it belongs to. The number is the larger of two values:

* the **statutory floor**, ``finance.retention.statutory_years``: national law,
  maintained by CodeX as a platform-only configuration value, never by a
  tenant. In Nigeria the usual requirement is **six years**: the Companies and
  Allied Matters Act 2020 (accounting records kept for at least six years from
  the date they are made), and the tax statutes (the Companies Income Tax Act
  and the Personal Income Tax Act, which let the revenue service assess back
  six years, and the VAT Act's record-keeping rule) all land on six. That
  figure is a reading of the law for the accountant and counsel to confirm,
  which is why it is data CodeX can change without a release.
* the tenant's own choice, ``finance.retention.years``: a tenant may keep its
  books longer, never shorter. A value below the floor is refused when it is
  written (:func:`guard_retention_years`), and a floor raised later wins over a
  lower value already stored, because the effective period is always
  ``max(floor, tenant value)``.

Bright Star School's FY2027 ends on 31 December 2027. With the six-year floor,
every journal, bill, receipt, payslip and return of 2027 is kept until
31 December 2033, so a tax review in 2033 finds every one of them.

**The rows.** A finance or procurement document is kept once it has left
draft: from then on it is a record of something that happened (a posting, an
approval, a cancellation), and a draft is only somebody's unfinished work.
The lines, payslips, issued copies and filed evidence that make up a kept
document are kept with it. :func:`register_policies` declares them all to
:mod:`core.retention`, which refuses every ORM deletion of a kept row and
keeps the bytes of every file filed against one. The audit trails are not
here: they are append-only for ever, enforced by database triggers.
"""
from __future__ import annotations

import datetime

from core.retention import Hold

#: CodeX's statutory floor, in years; platform scope only.
STATUTORY_YEARS_KEY = "finance.retention.statutory_years"
#: A tenant's own retention period, in years; may only lengthen the floor.
TENANT_YEARS_KEY = "finance.retention.years"
#: The floor used when the platform value is missing or unreadable.
DEFAULT_STATUTORY_YEARS = 6

#: The date that places a document in its fiscal year, where it is not the first date field.
_DATE_FIELD_OVERRIDES = {
    "vs_finance.TaxFiling": "period_end",
}


def _as_years(value):
    try:
        years = int(value)
    except (TypeError, ValueError):
        return None
    return years if years > 0 else None


def statutory_years() -> int:
    """The statutory floor in years, never less than one."""
    from vs_config.conf import get_config

    return _as_years(get_config(STATUTORY_YEARS_KEY, DEFAULT_STATUTORY_YEARS)) or (
        DEFAULT_STATUTORY_YEARS
    )


def retention_years(tenant) -> int:
    """The years ``tenant`` keeps its books: the floor, or its own longer choice."""
    from vs_config.conf import get_config

    floor = statutory_years()
    own = _as_years(get_config(TENANT_YEARS_KEY, None, tenant=tenant)) if tenant else None
    return max(floor, own or 0)


def guard_retention_years(value, *, tenant=None, branch=None) -> None:
    """Refuse a tenant value shorter than the statutory floor.

    Registered as the :mod:`vs_config` write guard for :data:`TENANT_YEARS_KEY`.
    Clearing the value (``None``) is allowed: it means "keep for the floor".
    """
    from vs_config.exceptions import ConfigurationError

    if value is None:
        return
    floor = statutory_years()
    years = _as_years(value)
    if years is None or years < floor:
        raise ConfigurationError(
            f"Financial records must be kept for at least {floor} years by law. "
            f"A school may keep them longer, never shorter.",
            extra={"field": "value", "minimum": floor},
        )


def _add_years(day: datetime.date, years: int) -> datetime.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February in a year that has none.
        return day.replace(year=day.year + years, day=28)


def fiscal_year_end(entity, day: datetime.date) -> datetime.date:
    """The last day of the fiscal year holding ``day``.

    A day outside every fiscal year the books have (before the first, or after
    the last) is placed in a calendar year, which is what a fiscal year is
    unless the tenant set another.
    """
    from .models import FiscalYear

    end = (
        FiscalYear.objects.filter(entity=entity, start_date__lte=day, end_date__gte=day)
        .values_list("end_date", flat=True).first()
    )
    return end or datetime.date(day.year, 12, 31)


def retained_until(entity, day: datetime.date) -> datetime.date:
    """The last day a record dated ``day`` in ``entity``'s books must be kept."""
    return _add_years(fiscal_year_end(entity, day), retention_years(entity.tenant))


def dated_record(date_attr, what):
    """A policy for a row that is a record from the day it exists, dated by ``date_attr``.

    The row names its books through ``entity`` or, failing that, its ``tenant``,
    whose own books place the date in a fiscal year.
    """

    def policy(row):
        from .models import LedgerEntity

        day = getattr(row, date_attr, None) or row.created_at
        entity = getattr(row, "entity", None)
        if entity is None and getattr(row, "tenant_id", None):
            entity = LedgerEntity.objects.filter(tenant_id=row.tenant_id).order_by("pk").first()
        return hold_for(entity, day, what)

    return policy


def hold_for(entity, day, what):
    """A :class:`~core.retention.Hold` while today is inside the period, else ``None``."""
    from vs_config.clock import tenant_today

    if entity is None:
        return None
    if isinstance(day, datetime.datetime):
        day = day.date()
    day = day or tenant_today(entity.tenant)
    until = retained_until(entity, day)
    return Hold(until=until, what=what) if tenant_today(entity.tenant) <= until else None


def document_date_field(model):
    """The name of the date field that places a ``model`` document in a fiscal year.

    The first date field a document declares is its own date (an invoice's
    invoice date, a payment's payment date); a tax return is placed by the end
    of the period it declares. ``None`` for a model with no date field.
    """
    from django.db import models

    name = _DATE_FIELD_OVERRIDES.get(model._meta.label)
    if name is not None:
        return name
    return next(
        (
            field.name for field in model._meta.concrete_fields
            if isinstance(field, models.DateField) and not isinstance(field, models.DateTimeField)
        ),
        None,
    )


def record_date(document):
    """The date that places ``document`` in a fiscal year, or the day it was created."""
    name = document_date_field(type(document))
    day = getattr(document, name, None) if name else None
    return day or document.created_at


def _label(document) -> str:
    number = getattr(document, "document_number", "") or f"#{document.pk}"
    return f"{document._meta.verbose_name} {number}"


def document_hold(document):
    """A finance or procurement document is kept once it has left draft."""
    from .constants import DocumentStatus

    if document.status == DocumentStatus.DRAFT:
        return None
    return hold_for(document.entity, record_date(document), _label(document))


def held_with(parent_attr):
    """A policy keeping a row exactly as long as the document it belongs to."""
    from core.retention import hold_on

    def policy(row):
        return hold_on(getattr(row, parent_attr, None))

    return policy


def _reconciliation_hold(reconciliation):
    return hold_for(
        reconciliation.bank_account.entity, reconciliation.as_of_date,
        "a bank reconciliation",
    )


def register_policies() -> None:
    """Declare every kept finance and procurement row to :mod:`core.retention`.

    Documents are found by type, so a new :class:`FinanceDocument` subclass in
    any app is kept the day it is added. Child rows are named: a line whose
    document is kept is kept, but child rows that are rebuilt as a document is
    worked (allocations, schedules, declaration links) are left to the
    services that own them.
    """
    from django.apps import apps

    from core import retention

    from .models import (
        BankReconciliation,
        ExpenseClaimLine,
        FinanceDocumentDelivery,
        InvoiceLine,
        JournalLine,
        PayrollLine,
        PayrollLineItem,
        Payslip,
    )
    from .models.core import FinanceDocument

    for model in apps.get_models():
        if issubclass(model, FinanceDocument):
            retention.register(model, document_hold)

    retention.register(JournalLine, held_with("entry"))
    retention.register(InvoiceLine, held_with("invoice"))
    retention.register(ExpenseClaimLine, held_with("claim"))
    retention.register(PayrollLine, held_with("run"))
    retention.register(PayrollLineItem, held_with("line"))
    retention.register(Payslip, held_with("run"))
    retention.register(BankReconciliation, _reconciliation_hold)
    retention.register(
        FinanceDocumentDelivery, dated_record("created_at", "an emailed finance document"),
    )
