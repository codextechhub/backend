"""Opening fiscal years, and keeping an entity's calendar ahead of today.

Every posting needs a :class:`~vs_finance.models.FiscalPeriod` covering its date,
and periods exist only as far as the last fiscal year somebody opened. When the
calendar runs out, or when two years leave a stretch between them, every posting
dated there fails at once. This module holds the two things that prevent that:

* :func:`open_fiscal_year` is the one way a year is opened, by a person through the
  fiscal-year endpoint or by the rollover below. It refuses a year that overlaps
  another or that leaves a gap beside its neighbours, and it audits the opening.
* :func:`roll_fiscal_calendar` runs daily for each entity (see
  :func:`vs_finance.tasks.roll_fiscal_calendars`). Within the entity's lead window
  it opens the next year itself, or only tells the finance staff, as
  :class:`~vs_finance.models.FinanceCalendarSettings` says. Whenever the calendar
  still needs a person afterwards - warn-only, an automatic opening that failed, a
  gap no new year at the end can fill - the staff who may open a year are told
  (:func:`calendar_alert_recipients`).

Nothing here knows what kind of organisation keeps the books. The start month and
period length of a new year are read from the year before it, so a September-to-
August entity rolls into another September-to-August year.
"""
from __future__ import annotations

from vs_finance.wording import counted

import datetime
import logging

from django.db import transaction
from rest_framework.exceptions import ValidationError

from vs_config.clock import tenant_today, tenant_zone
from vs_config.display import format_date

from .constants import FinanceAuditAction

logger = logging.getLogger(__name__)

#: The permission whose holders are told the calendar needs them: the people who
#: may open a fiscal year, since opening one is the fix.
CALENDAR_ALERT_PERMISSION = "finance.period.create"

#: The notification event the warning is sent as.
CALENDAR_ALERT_EVENT = "finance.fiscal_calendar_expiring"

#: The most years one rollover run opens for one entity. A calendar that lapsed
#: long ago catches up over a few days rather than in one unbounded loop.
MAX_YEARS_PER_RUN = 3

#: Days between repeat warnings about the same break in the calendar. Weekly while
#: there is time, daily in the last week and once postings have started failing.
WARNING_INTERVAL_DAYS = 7
URGENT_WARNING_INTERVAL_DAYS = 1
URGENT_WITHIN_DAYS = 7

_ONE_DAY = datetime.timedelta(days=1)


def _period_frequency(fiscal_year) -> str:
    """``QUARTERLY`` when the year has four regular periods, else ``MONTHLY``."""
    regular = fiscal_year.periods.filter(period_no__lte=12).count()
    return "QUARTERLY" if regular == 4 else "MONTHLY"


def next_fiscal_year_spec(entity) -> dict | None:
    """The year that continues ``entity``'s calendar without a gap, or ``None``.

    It starts the day after the latest year ends, so its label, start month and
    start day all come from that date, and it keeps the latest year's period
    length. ``None`` when the entity has no year to continue from.
    """
    from .models import FiscalYear

    latest = FiscalYear.objects.filter(entity=entity).order_by("-end_date").first()
    if latest is None:
        return None
    start = latest.end_date + _ONE_DAY
    return {
        "year": start.year,
        "start_month": start.month,
        "start_day": start.day,
        "frequency": _period_frequency(latest),
    }


def _refuse_gaps(entity, fiscal_year) -> None:
    """Refuse a new year that leaves uncovered days beside its neighbours."""
    from .models import FiscalYear

    others = FiscalYear.objects.filter(entity=entity).exclude(pk=fiscal_year.pk)

    def day(value):
        return format_date(value, entity.tenant)

    previous = others.filter(end_date__lt=fiscal_year.start_date).order_by("-end_date").first()
    if previous is not None and previous.end_date + _ONE_DAY != fiscal_year.start_date:
        raise ValidationError({
            "fiscal_calendar": (
                f"FY{fiscal_year.year} would start on {day(fiscal_year.start_date)}, leaving "
                f"{day(previous.end_date + _ONE_DAY)} to {day(fiscal_year.start_date - _ONE_DAY)} "
                f"uncovered after FY{previous.year} ends on {day(previous.end_date)}. Nothing "
                f"could be posted in that stretch. Start the year on "
                f"{day(previous.end_date + _ONE_DAY)}."
            ),
        })
    following = others.filter(start_date__gt=fiscal_year.end_date).order_by("start_date").first()
    if following is not None and fiscal_year.end_date + _ONE_DAY != following.start_date:
        raise ValidationError({
            "fiscal_calendar": (
                f"FY{fiscal_year.year} would end on {day(fiscal_year.end_date)}, leaving "
                f"{day(fiscal_year.end_date + _ONE_DAY)} to {day(following.start_date - _ONE_DAY)} "
                f"uncovered before FY{following.year} starts on {day(following.start_date)}. "
                f"Nothing could be posted in that stretch."
            ),
        })


@transaction.atomic
def open_fiscal_year(entity, *, year, start_month, start_day, frequency,
                     actor_user=None, automatic=False):
    """Open fiscal year ``year`` with its complete set of periods.

    The entity row is locked first, so two requests (or a request and the daily
    rollover) cannot both pass the checks and open the same year. Refused, with the
    whole opening rolled back, when the year already exists, overlaps another, or
    leaves a gap before or after its neighbours: a gap is a stretch in which every
    posting fails, and it is invisible until somebody tries to post there.

    Returns ``(fiscal_year, periods)``. ``automatic`` marks the audit row as opened
    by the rollover rather than by a person.
    """
    from .audit import record
    from .models import FiscalYear, LedgerEntity
    from .seed import seed_fiscal_year

    LedgerEntity.objects.select_for_update().get(pk=entity.pk)
    if FiscalYear.objects.filter(entity=entity, year=year).exists():
        raise ValidationError({
            "year": f"Fiscal year {year} already exists for this entity.",
        })
    try:
        fiscal_year, periods = seed_fiscal_year(
            entity,
            year=year,
            start_month=start_month,
            fiscal_period_frequency=frequency,
            fiscal_start_day=start_day,
        )
    except ValueError as exc:
        raise ValidationError({"fiscal_calendar": str(exc)}) from exc

    overlap = (
        FiscalYear.objects.filter(
            entity=entity,
            start_date__lte=fiscal_year.end_date,
            end_date__gte=fiscal_year.start_date,
        )
        .exclude(pk=fiscal_year.pk)
        .order_by("start_date")
        .first()
    )
    if overlap is not None:
        raise ValidationError({
            "fiscal_calendar": (
                f"FY{year} overlaps FY{overlap.year} "
                f"({format_date(overlap.start_date, entity.tenant)} to "
                f"{format_date(overlap.end_date, entity.tenant)})."
            ),
        })
    _refuse_gaps(entity, fiscal_year)

    record(
        entity=entity,
        action=FinanceAuditAction.FISCAL_YEAR_OPENED,
        actor_user=actor_user,
        target=fiscal_year,
        target_type="FiscalYear",
        message=(
            f"Opened FY{year} ({format_date(fiscal_year.start_date, entity.tenant)} to "
            f"{format_date(fiscal_year.end_date, entity.tenant)})"
            f"{' automatically' if automatic else ''}."
        ),
        after={
            "start_date": fiscal_year.start_date.isoformat(),
            "end_date": fiscal_year.end_date.isoformat(),
            "frequency": frequency,
        },
        fiscal_year=year,
        automatic=automatic,
    )
    return fiscal_year, periods


@transaction.atomic
def open_next_fiscal_year(entity, *, actor_user=None, unless_covered_beyond=None,
                          automatic=False):
    """Open the year that continues ``entity``'s calendar, or return ``None``.

    The spec is read under the entity lock. With ``unless_covered_beyond``, nothing
    is opened when the calendar already runs past that date, which is what makes the
    rollover safe to run twice at once: the second run waits for the first, then
    sees the year it opened and stops, rather than opening the year after that too.
    ``None`` as well when the entity has no year to continue from.
    """
    from .models import LedgerEntity

    LedgerEntity.objects.select_for_update().get(pk=entity.pk)
    spec = next_fiscal_year_spec(entity)
    if spec is None:
        return None
    last_day = datetime.date(spec["year"], spec["start_month"], spec["start_day"]) - _ONE_DAY
    if unless_covered_beyond is not None and last_day > unless_covered_beyond:
        return None
    fiscal_year, _periods = open_fiscal_year(
        entity, actor_user=actor_user, automatic=automatic, **spec,
    )
    return fiscal_year


def roll_fiscal_calendar(entity, *, today=None) -> dict:
    """Keep ``entity``'s calendar ahead of today, then warn if it still needs a person.

    Under AUTO_OPEN, while the last year ends within the lead window, the next
    contiguous year is opened (at most :data:`MAX_YEARS_PER_RUN` per run). Then the
    runway is read again, and if it still says anything other than HEALTHY the
    finance staff are warned: always under WARN_ONLY, and under AUTO_OPEN when the
    opening failed, when there is no year to continue from, or when the break is a
    gap between two years that no new year at the end can fill.

    Safe to run any number of times a day. A year is opened only while the calendar
    is short, and a warning about the same break is repeated only on the interval
    :func:`_warning_due` allows.

    Returns ``{"opened": [years], "warned": bool, "failure": str}``.
    """
    from .calendar_settings import resolve_finance_calendar_settings
    from .models import FinanceCalendarSettings
    from .posting import fiscal_calendar_runway

    settings = resolve_finance_calendar_settings(entity)
    today = today or tenant_today(entity.tenant)
    horizon = today + datetime.timedelta(days=settings.next_year_lead_days)
    opened: list[int] = []
    failure = ""

    if settings.next_year_mode == FinanceCalendarSettings.NextYearMode.AUTO_OPEN:
        for _ in range(MAX_YEARS_PER_RUN):
            try:
                fiscal_year = open_next_fiscal_year(
                    entity, unless_covered_beyond=horizon, automatic=True,
                )
            except Exception as exc:  # noqa: BLE001 - reported to the staff below
                failure = _describe_failure(exc)
                logger.warning(
                    "Could not open the next fiscal year for entity %s: %s",
                    entity.code, failure,
                )
                break
            if fiscal_year is None:
                break
            opened.append(fiscal_year.year)

    runway = fiscal_calendar_runway(entity, today=today)
    warned = False
    if runway["should_warn"]:
        warned = _warn(entity, runway, settings=settings, failure=failure, today=today)
    return {"opened": opened, "warned": warned, "failure": failure}


def _describe_failure(exc) -> str:
    """One readable line from whatever stopped an automatic opening."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        parts = []
        for value in detail.values():
            parts.extend(str(item) for item in (value if isinstance(value, list) else [value]))
        return " ".join(parts) or str(exc)
    if isinstance(detail, list):
        return " ".join(str(item) for item in detail)
    return str(exc) or type(exc).__name__


def _warning_due(entity, runway, *, today) -> bool:
    """Whether a warning about this break in the calendar is due today.

    Keyed on the date postings stop, so a new break (a year opened but a gap still
    ahead) is reported at once, while the same break is repeated weekly and then
    daily in its last week. Days are the entity's own calendar days, so a run just
    after midnight UTC does not count as a new day in Lagos.

    The day a warning went out is the ``today`` the rollover ran for, stored on the
    audit row as ``warned_on``, so the interval is measured on one clock: the one
    every other decision in :func:`roll_fiscal_calendar` uses. A row without it
    falls back to its own timestamp read in the entity's time zone.
    """
    from .models import FinanceAuditLog

    last = (
        FinanceAuditLog.objects
        .filter(
            entity=entity,
            action=FinanceAuditAction.FISCAL_CALENDAR_WARNED,
            metadata__first_uncovered_date=runway["first_uncovered_date"].isoformat(),
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if last is None:
        return True
    days_remaining = runway["days_remaining"]
    interval = (
        URGENT_WARNING_INTERVAL_DAYS
        if days_remaining is None or days_remaining <= URGENT_WITHIN_DAYS
        else WARNING_INTERVAL_DAYS
    )
    warned_on = (last.metadata or {}).get("warned_on")
    last_day = (
        datetime.date.fromisoformat(warned_on) if warned_on
        else last.created_at.astimezone(tenant_zone(entity.tenant)).date()
    )
    return (today - last_day).days >= interval


def _situation(runway, tenant) -> str:
    """The sentence that says what is wrong with the calendar, in *tenant*'s date format."""
    breaks = runway["first_uncovered_date"]
    if runway["calendar_end"] is None:
        return "No fiscal year has been opened, so nothing can be posted."
    if breaks <= runway["calendar_end"]:
        gap = next(
            (gap for gap in runway["gaps"] if gap["start"] <= breaks <= gap["end"]),
            None,
        )
        if gap is None:  # Today falls before the first period.
            return (
                f"No fiscal period covers {format_date(breaks, tenant)}, so nothing dated "
                f"today can be posted."
            )
        return (
            f"No fiscal period covers {format_date(breaks, tenant)} to "
            f"{format_date(gap['end'], tenant)}, so nothing dated in that stretch can be posted."
        )
    if runway["days_remaining"] is not None and runway["days_remaining"] < 0:
        return (
            f"The fiscal calendar ended on {format_date(runway['calendar_end'], tenant)}, so "
            f"nothing dated after it can be posted."
        )
    return (
        f"The fiscal calendar ends on {format_date(runway['calendar_end'], tenant)}, and "
        f"nothing dated after it can be posted until the next year is opened."
    )


def _action(runway, *, settings, failure) -> str:
    """The sentence that says what the reader should do about it."""
    from .models import FinanceCalendarSettings

    if runway["calendar_end"] is not None and (
        runway["first_uncovered_date"] <= runway["calendar_end"]
    ):
        return "Open a fiscal year covering that stretch in Finance settings."
    if failure:
        return (
            f"Opening the next fiscal year automatically failed: {failure} "
            f"Open it by hand in Finance settings."
        )
    if settings.next_year_mode == FinanceCalendarSettings.NextYearMode.WARN_ONLY:
        return "Open the next fiscal year in Finance settings before then."
    return "Open a fiscal year in Finance settings."


def calendar_alert_recipients(tenant) -> list:
    """The people told the calendar needs them: those who can open a year.

    Opening a year is a write to the fiscal calendar, which carries no branch,
    so it takes both the key (:data:`CALENDAR_ALERT_PERMISSION`) and whole-tenant
    reach (:func:`vs_rbac.scoping.caller_reaches_whole_tenant`), the same pair
    the fiscal-year endpoint demands. A warning is only useful to somebody who
    can act on it: Lekki's bursar holds the key through a role pinned to Lekki,
    the endpoint would refuse her, and telling her daily that postings stop
    would leave her with an alarm and no way to answer it. At a school with one
    branch, a bursar pinned to it reaches the whole school and is told.

    Holders are found at any branch and then judged on their whole reach, not
    on the grant carrying the key, because that is how the endpoint judges them.
    """
    from vs_rbac.evaluator import ANY_BRANCH, resolve_users_with_permission
    from vs_rbac.scoping import caller_reaches_whole_tenant, visible_branch_ids_for

    holders = list(resolve_users_with_permission(
        tenant, ANY_BRANCH, CALENDAR_ALERT_PERMISSION,
    ))
    reach = visible_branch_ids_for(holders, tenant)
    return [
        user for user in holders
        if (caller_reaches_whole_tenant(user, tenant, visible=reach[user.pk])
            if user.pk in reach else caller_reaches_whole_tenant(user, tenant))
    ]


def _warn(entity, runway, *, settings, failure, today) -> bool:
    """Tell the staff who may open a year, and record that they were told.

    Delivery goes through :mod:`vs_notifications` and never raises into the
    rollover. The audit row is written only when somebody was actually notified, so
    a tenant where nobody holds the permission is retried rather than marked done.
    """
    from .audit import record

    if not _warning_due(entity, runway, today=today):
        return False
    try:
        from vs_notifications.notify import send_notification

        recipients = calendar_alert_recipients(entity.tenant)
        if not recipients:
            logger.warning(
                "%s: nobody at entity %s holds %s school-wide.",
                CALENDAR_ALERT_EVENT, entity.code, CALENDAR_ALERT_PERMISSION,
            )
            return False
        days_remaining = runway["days_remaining"]
        notification_ids = send_notification(
            CALENDAR_ALERT_EVENT,
            context={
                "entity_name": entity.name,
                "entity_code": entity.code,
                "first_uncovered_date": format_date(runway["first_uncovered_date"], entity.tenant),
                "calendar_end": format_date(runway["calendar_end"], entity.tenant),
                "days_remaining": days_remaining if days_remaining is not None else "",
                "situation": _situation(runway, entity.tenant),
                "action": _action(runway, settings=settings, failure=failure),
            },
            recipients=recipients,
            tenant=entity.tenant,
        )
    except Exception:  # noqa: BLE001 - a warning must never break the rollover
        logger.exception("%s: could not warn entity %s.", CALENDAR_ALERT_EVENT, entity.code)
        return False
    if not notification_ids:
        return False
    record(
        entity=entity,
        action=FinanceAuditAction.FISCAL_CALENDAR_WARNED,
        target_type="LedgerEntity",
        target_id=str(entity.pk),
        message=(
            f"Warned {counted(len(recipients), 'finance user')} that postings stop on "
            f"{format_date(runway['first_uncovered_date'], entity.tenant)}."
        ),
        first_uncovered_date=runway["first_uncovered_date"].isoformat(),
        days_remaining=days_remaining,
        failure=failure,
        warned_on=today.isoformat(),
    )
    return True
