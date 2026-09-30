"""Deferred income: billed ahead of the service, earned month by month.

A bill raised before the service it pays for is not income yet. When an invoice
posts, a ``CHARGE`` line whose service period starts after the invoice date
credits the deferred-income liability instead of revenue, and
:func:`schedule_line` writes its :class:`~vs_finance.models.DeferredIncomeEntry`
rows: one per month of the service period under ``SPREAD_MONTHLY`` (equal whole-kobo
shares, the remainder in the last month), or one at the service start under
``AT_PERIOD_START``. A line with no service period, or whose period has already
begun, is revenue on the invoice date; there is no "on billing" option.

Greenfield bills Second Term (6 January to 4 April 2027) on 10 December 2026:
N150,000 per child. December's books show a N150,000 liability and no income;
January, February, March and April each receive N37,500 when their month is
released, so the 2026 accounts carry none of that term's fees.

:func:`release_deferred_income` moves every share due by a date to revenue, one
journal per branch per month (a :class:`~vs_finance.models.DeferredIncomeRelease`).
It is idempotent, runs on demand and as a step of period close, and a month cannot
close while a share due in it is unreleased (:func:`deferred_income_close_check`).
:func:`reverse_deferred_release` undoes a month's releases while that month is open.

A credit note, concession or write-off of a deferred bill takes back the part not
yet released first (:func:`plan_unwind` / :func:`apply_unwind`): its journal debits
the liability rather than revenue for that part, latest months first, and voiding
the adjustment restores it (:func:`restore_unwinds`). Voiding the invoice cancels
what is still pending and takes the revenue already released back out
(:func:`cancel_invoice_schedule`).
"""
from __future__ import annotations

import calendar
import datetime
from collections import defaultdict

from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from .account_mappings import resolve_mapped_account
from .audit import record
from .constants import (
    AccountMappingKey,
    ChargeKind,
    DeferredIncomeStatus,
    FinanceAuditAction,
    JournalSource,
    PeriodStatus,
    RevenueRecognitionMethod,
)
from .exceptions import PeriodCloseError, PostingError


def month_end(day: datetime.date) -> datetime.date:
    """The last day of the month ``day`` falls in."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _next_month(day: datetime.date) -> datetime.date:
    return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def recognition_schedule(net: int, start: datetime.date, end: datetime.date,
                         method: str) -> list[tuple[datetime.date, int]]:
    """``[(recognition_date, kobo)]`` for ``net`` earned over ``start``-``end``.

    ``SPREAD_MONTHLY`` gives each calendar month the period touches an equal share
    in whole kobo, and the last month the remainder, so the shares always add up to
    ``net``. The first share is recognised on ``start``, each later one on the first
    of its month. ``AT_PERIOD_START`` recognises all of it on ``start``.
    """
    if net <= 0:
        return []
    if method == RevenueRecognitionMethod.AT_PERIOD_START:
        return [(start, net)]
    months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append(cursor)
        cursor = _next_month(cursor)
    share, remainder = divmod(net, len(months))
    schedule = []
    for index, first in enumerate(months):
        amount = share + (remainder if index == len(months) - 1 else 0)
        if amount:
            schedule.append((start if index == 0 else first, amount))
    return schedule


def defers(line, invoice_date) -> bool:
    """Whether posting ``line`` defers its income: a charge whose service starts later."""
    return (
        line.kind == ChargeKind.CHARGE
        and line.service_start is not None
        and line.service_start > invoice_date
    )


def schedule_line(invoice, line, *, method) -> list:
    """Write the monthly shares of one deferred invoice line. Returns the entries."""
    from .models import DeferredIncomeEntry

    return DeferredIncomeEntry.objects.bulk_create([
        DeferredIncomeEntry(
            entity_id=invoice.entity_id, branch_id=invoice.branch_id,
            invoice=invoice, line=line, revenue_account_id=line.revenue_account_id,
            cost_center_id=line.cost_center_id, recognition_date=day, amount=amount,
        )
        for day, amount in recognition_schedule(
            int(line.net_amount), line.service_start, line.service_end, method)
    ])


def pending_total(invoice) -> int:
    """Deferred income of ``invoice`` not yet released, in kobo."""
    from .models import DeferredIncomeEntry

    row = DeferredIncomeEntry.objects.filter(
        invoice=invoice, status=DeferredIncomeStatus.PENDING,
    ).aggregate(total=Sum(F("amount") - F("unwound_amount")))
    return int(row["total"] or 0)


# --------------------------------------------------------------------------- #
# Adjustments of a deferred bill                                               #
# --------------------------------------------------------------------------- #

def plan_unwind(invoice, amount) -> list[tuple[object, int]]:
    """The pending shares an adjustment of ``amount`` takes back, latest month first.

    Locks the shares, so two adjustments of one bill queue. The latest months go
    first because an adjustment of a bill in progress is almost always about the
    service still to come: Tunde withdraws in February and the March and April
    shares are the ones he will not receive.
    """
    from .models import DeferredIncomeEntry

    remaining = int(amount or 0)
    if remaining <= 0 or invoice is None:
        return []
    plan = []
    for entry in (DeferredIncomeEntry.objects.select_for_update()
                  .filter(invoice=invoice, status=DeferredIncomeStatus.PENDING)
                  .order_by("-recognition_date", "-id")):
        if remaining <= 0:
            break
        take = min(entry.open_amount, remaining)
        if take > 0:
            plan.append((entry, take))
            remaining -= take
    return plan


def apply_unwind(plan, *, adjustment_entry) -> int:
    """Record ``plan`` against the adjusting journal that debited the liability for it."""
    from .models import DeferredIncomeUnwind

    for entry, take in plan:
        entry.unwound_amount += take
        if entry.open_amount == 0:
            entry.status = DeferredIncomeStatus.CANCELLED
        entry.save(update_fields=["unwound_amount", "status", "updated_at"])
        DeferredIncomeUnwind.objects.create(
            entry=entry, adjustment_entry=adjustment_entry, amount=take,
        )
    return sum(take for _entry, take in plan)


def restore_unwinds(adjustment_entry) -> int:
    """Give back what a voided adjustment took from the schedule. Returns the kobo restored.

    A share still waiting gets its amount back. A share released since (it was
    only partly taken back, and the rest was released) cannot reopen, so the
    restored amount becomes a new share of its own on the same date, released with
    the next run.
    """
    from .models import DeferredIncomeEntry, DeferredIncomeUnwind

    restored = 0
    for unwind in (DeferredIncomeUnwind.objects.select_for_update()
                   .filter(adjustment_entry=adjustment_entry, restored=False)
                   .select_related("entry").order_by("pk")):
        entry = unwind.entry
        if entry.status == DeferredIncomeStatus.RELEASED:
            DeferredIncomeEntry.objects.create(
                entity_id=entry.entity_id, branch_id=entry.branch_id,
                invoice_id=entry.invoice_id, line_id=entry.line_id,
                revenue_account_id=entry.revenue_account_id,
                cost_center_id=entry.cost_center_id,
                recognition_date=entry.recognition_date, amount=unwind.amount,
            )
        else:
            entry.unwound_amount -= unwind.amount
            entry.status = DeferredIncomeStatus.PENDING
            entry.save(update_fields=["unwound_amount", "status", "updated_at"])
        unwind.restored = True
        unwind.save(update_fields=["restored", "updated_at"])
        restored += int(unwind.amount)
    return restored


def cancel_invoice_schedule(invoice, *, reversal, actor_user=None):
    """Cancel a voided invoice's schedule, taking back revenue already released.

    The void reverses the invoice journal, which debits the liability for every
    share it deferred. Shares still waiting are simply cancelled; for shares
    already released, one journal on the invoice's branch and the reversal's date
    takes that revenue back (``Dr revenue, Cr deferred income``), so the pair nets
    the liability to nothing. Returns that journal, or ``None``.
    """
    from .models import DeferredIncomeEntry, JournalEntry, JournalLine
    from .posting import post_journal, resolve_period

    entries = list(DeferredIncomeEntry.objects.select_for_update()
                   .filter(invoice=invoice).order_by("pk"))
    if not entries:
        return None
    released: dict[tuple[int, int | None], int] = defaultdict(int)
    for entry in entries:
        if entry.status == DeferredIncomeStatus.RELEASED:
            released[(entry.revenue_account_id, entry.cost_center_id)] += int(entry.released_amount)
    DeferredIncomeEntry.objects.filter(
        invoice=invoice, status=DeferredIncomeStatus.PENDING,
    ).update(status=DeferredIncomeStatus.CANCELLED, updated_at=timezone.now())
    total = sum(released.values())
    if total <= 0:
        return None
    journal = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch, date=reversal.date,
        period=resolve_period(invoice.entity, reversal.date), source=JournalSource.SALES,
        narration=f"Released revenue of voided invoice {invoice.document_number}",
        reference=invoice.reference, created_by=actor_user,
    )
    line_no = 0
    for (account_id, cost_center_id), amount in released.items():
        if amount:
            line_no += 1
            JournalLine.objects.create(
                entry=journal, account_id=account_id, cost_center_id=cost_center_id,
                debit=amount, credit=0, description="Revenue of voided invoice",
                line_no=line_no,
            )
    JournalLine.objects.create(
        entry=journal, credit=total, debit=0, line_no=line_no + 1,
        account=resolve_mapped_account(invoice.entity, AccountMappingKey.DEFERRED_INCOME),
        description="Deferred income",
    )
    post_journal(journal, actor_user=actor_user)
    DeferredIncomeEntry.objects.filter(
        invoice=invoice, status=DeferredIncomeStatus.RELEASED,
    ).update(void_journal=journal, updated_at=timezone.now())
    return journal


# --------------------------------------------------------------------------- #
# Release                                                                     #
# --------------------------------------------------------------------------- #

def _release_date(entity, day, up_to, *, allow_restricted):
    """The date a share recognised on ``day`` is released on, by a run up to ``up_to``.

    The end of its own month, or ``up_to`` when that is earlier. A share whose
    month no longer takes postings (closed over it) is released on ``up_to``
    instead, in the month the run is for.
    """
    from .posting import _period_accepts_posting, resolve_period

    target = min(month_end(day), up_to)
    period = resolve_period(entity, target)
    if not _period_accepts_posting(period, allow_restricted=allow_restricted):
        return up_to
    return target


@transaction.atomic
def release_deferred_income(entity, *, up_to, actor_user=None, allow_restricted=False):
    """Move every share recognised on or before ``up_to`` to revenue. Returns the releases.

    One journal per branch per month: ``Dr deferred income``, ``Cr revenue`` per
    revenue account and cost centre, each naming its branch (a share keeps its
    invoice's). Idempotent: only waiting shares are released, so a second run the
    same day posts nothing. ``allow_restricted`` lets the period-close step post
    into a soft-closed month, as depreciation does.
    """
    from .models import DeferredIncomeEntry, DeferredIncomeRelease, JournalEntry, JournalLine
    from .posting import post_journal, resolve_period

    entries = list(
        DeferredIncomeEntry.objects.select_for_update()
        .filter(entity=entity, status=DeferredIncomeStatus.PENDING, recognition_date__lte=up_to)
        .filter(amount__gt=F("unwound_amount"))
        .order_by("recognition_date", "pk")
    )
    if not entries:
        return []
    groups: dict[tuple, list] = defaultdict(list)
    dates: dict[tuple, datetime.date] = {}
    for entry in entries:
        day = _release_date(entity, entry.recognition_date, up_to,
                            allow_restricted=allow_restricted)
        key = (entry.branch_id, day.year, day.month)
        groups[key].append(entry)
        dates[key] = max(dates.get(key, day), day)

    deferred = resolve_mapped_account(entity, AccountMappingKey.DEFERRED_INCOME)
    releases = []
    for key in sorted(groups, key=lambda k: (k[1], k[2], k[0] or 0)):
        rows, day = groups[key], dates[key]
        journal = JournalEntry.objects.create(
            entity=entity, branch_id=key[0], date=day, period=resolve_period(entity, day),
            source=JournalSource.CLOSING,
            narration=f"Deferred income released for {day:%B %Y}", created_by=actor_user,
        )
        revenue: dict[tuple[int, int | None], int] = defaultdict(int)
        for row in rows:
            revenue[(row.revenue_account_id, row.cost_center_id)] += row.open_amount
        total = sum(revenue.values())
        JournalLine.objects.create(
            entry=journal, account=deferred, debit=total, credit=0,
            description="Deferred income released", line_no=1,
        )
        for line_no, ((account_id, cost_center_id), amount) in enumerate(revenue.items(), start=2):
            JournalLine.objects.create(
                entry=journal, account_id=account_id, cost_center_id=cost_center_id,
                debit=0, credit=amount, description="Revenue earned", line_no=line_no,
            )
        post_journal(journal, actor_user=actor_user, allow_restricted=allow_restricted)
        release = DeferredIncomeRelease.objects.create(
            entity=entity, branch_id=key[0], journal=journal, amount=total,
            created_by=actor_user,
        )
        for row in rows:
            row.released_amount = row.open_amount
            row.status = DeferredIncomeStatus.RELEASED
            row.release = release
        DeferredIncomeEntry.objects.bulk_update(
            rows, ["released_amount", "status", "release", "updated_at"], batch_size=500)
        record(
            entity=entity, action=FinanceAuditAction.DEFERRED_INCOME_RELEASED,
            actor_user=actor_user, target=release,
            message=f"Released {total} kobo of deferred income for {day:%B %Y}.",
            journal_id=journal.pk, amount=total, branch_id=key[0],
            invoices=sorted({row.invoice_id for row in rows}),
        )
        releases.append(release)
    return releases


@transaction.atomic
def reverse_deferred_release(entity, period, *, actor_user=None):
    """Undo every release dated in ``period`` while it is open. Returns the count.

    Each release journal is reversed on its own date and its shares wait to be
    released again. A closed month keeps its releases: they are sealed with it. A
    release holding a share of an invoice voided since is refused, because the void
    has already taken that revenue back and reversing the release would take it a
    second time.
    """
    from .models import DeferredIncomeEntry, DeferredIncomeRelease
    from .posting import reverse_journal

    if period.entity_id != entity.pk:
        raise PostingError("That period belongs to another set of books.")
    if period.status != PeriodStatus.OPEN:
        raise PeriodCloseError(
            f"Period '{period}' is '{period.status}'; its deferred income releases are "
            f"sealed with it. Reopen the period first.",
        )
    releases = list(
        DeferredIncomeRelease.objects.select_for_update()
        .filter(entity=entity, reversed_at__isnull=True,
                journal__date__gte=period.start_date, journal__date__lte=period.end_date)
        .select_related("journal").order_by("pk")
    )
    for release in releases:
        voided = release.entries.filter(void_journal__isnull=False).values_list(
            "invoice__document_number", flat=True).first()
        if voided:
            raise PostingError(
                f"This release includes invoice {voided}, voided since; the void has "
                f"already taken its revenue back, so the release cannot be reversed.",
            )
        reversal = reverse_journal(
            release.journal, actor_user=actor_user, date=release.journal.date,
            document_owner=release,
        )
        release.entries.update(
            status=DeferredIncomeStatus.PENDING, released_amount=0, release=None,
            updated_at=timezone.now(),
        )
        release.reversed_at = timezone.now()
        release.save(update_fields=["reversed_at", "updated_at"])
        record(
            entity=entity, action=FinanceAuditAction.DEFERRED_RELEASE_REVERSED,
            actor_user=actor_user, target=release,
            message=f"Reversed a deferred income release of {release.amount} kobo.",
            journal_id=release.journal_id, reversal_id=reversal.pk,
        )
    return len(releases)


def deferred_income_close_check(entity, period):
    """Close check: no deferred income due in or before ``period`` is left unreleased."""
    from .close import ChecklistItem
    from .models import DeferredIncomeEntry

    due = DeferredIncomeEntry.objects.filter(
        entity=entity, status=DeferredIncomeStatus.PENDING,
        recognition_date__lte=period.end_date,
    ).filter(amount__gt=F("unwound_amount"))
    row = due.aggregate(n=Count("pk"), total=Sum(F("amount") - F("unwound_amount")))
    count, total = int(row["n"] or 0), int(row["total"] or 0)
    return ChecklistItem(
        name="deferred_income_released", passed=count == 0,
        detail=f"{count} deferred income share(s) due, {total} kobo, not yet released",
    )


deferred_income_close_check.check_name = "deferred_income_released"


def invoice_deferred_totals(invoice) -> dict:
    """One invoice's deferred income, in kobo: still waiting, and released to revenue."""
    from django.db.models import Q

    from .models import DeferredIncomeEntry

    row = DeferredIncomeEntry.objects.filter(invoice=invoice).aggregate(
        pending=Sum(F("amount") - F("unwound_amount"),
                    filter=Q(status=DeferredIncomeStatus.PENDING)),
        released=Sum("released_amount", filter=Q(status=DeferredIncomeStatus.RELEASED)),
    )
    return {"pending": int(row["pending"] or 0), "released": int(row["released"] or 0)}


def deferred_income_summary(entity, *, scope=None) -> dict:
    """Deferred income per state for a reader, in kobo: waiting, released, next due."""
    from .models import DeferredIncomeEntry

    rows = DeferredIncomeEntry.objects.filter(entity=entity)
    if scope is not None:
        rows = scope.filter(rows)
    pending = rows.filter(status=DeferredIncomeStatus.PENDING)
    totals = pending.aggregate(total=Sum(F("amount") - F("unwound_amount")))
    released = rows.filter(status=DeferredIncomeStatus.RELEASED).aggregate(
        total=Sum("released_amount"))
    upcoming = (
        pending.filter(amount__gt=F("unwound_amount"))
        .values("recognition_date__year", "recognition_date__month")
        .annotate(total=Sum(F("amount") - F("unwound_amount")))
        .order_by("recognition_date__year", "recognition_date__month")[:12]
    )
    return {
        "pending": int(totals["total"] or 0),
        "released": int(released["total"] or 0),
        "by_month": [
            {"month": f"{row['recognition_date__year']:04d}-{row['recognition_date__month']:02d}",
             "amount": int(row["total"] or 0)}
            for row in upcoming
        ],
    }

