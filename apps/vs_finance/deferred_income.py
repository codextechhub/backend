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

Every share is held by one branch: the invoice's, until a receivable move carries
the months still to come to another branch. A credit note or concession takes
each share back from the branch that holds it, and a write-off only the shares
its own branch holds (:mod:`vs_finance.bill_adjustments` says which branch's
books each part of an adjustment comes out of).
"""
from __future__ import annotations

import calendar
import datetime
from collections import defaultdict
from typing import NamedTuple

from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from .account_mappings import resolve_mapped_account
from .audit import record
from .constants import (
    AccountMappingKey,
    ChargeKind,
    DeferredIncomeStatus,
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    PeriodStatus,
    RevenueRecognitionMethod,
)
from .exceptions import PeriodCloseError, PostingError
from .money import format_naira


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

class UnwindStep(NamedTuple):
    """One share an adjustment takes back, and how much of it.

    ``after_release`` marks a share taken back out of the revenue it already
    released (:func:`plan_released_takeback`); otherwise the share is still
    waiting, and the liability is what the adjustment debits.
    """

    entry: object
    take: int
    after_release: bool = False


def held_elsewhere(entry, branch_id) -> bool:
    """Whether the share ``entry`` sits at a branch other than ``branch_id``.

    Both must name a branch. A share or an adjusting journal not yet given one is
    taken as the adjusting journal's own, as every share was before a receivable
    move could carry part of a bill to another branch.
    """
    return entry.branch_id is not None and branch_id is not None and entry.branch_id != branch_id


def plan_unwind(invoice, amount, *, held_by=None) -> list[UnwindStep]:
    """The pending shares an adjustment of ``amount`` takes back, latest month first.

    Locks the shares, so two adjustments of one bill queue. The latest months go
    first because an adjustment of a bill in progress is almost always about the
    service still to come: Tunde withdraws in February and the March and April
    shares are the ones he will not receive.

    The shares may sit at more than one branch once a receivable move has carried
    the bill (:func:`vs_finance.inter_branch.transfer_open_receivables`): the
    months still to come move, and a month already earned stays at the branch
    that earned it. The plan takes them in the same order wherever they sit,
    unless ``held_by`` names the only branch whose shares may be taken, as a
    write-off does (:mod:`vs_finance.bill_adjustments`).
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
        if held_by is not None and held_elsewhere(entry, held_by):
            continue
        take = min(entry.open_amount, remaining)
        if take > 0:
            plan.append(UnwindStep(entry, take))
            remaining -= take
    return plan


def shares_elsewhere(invoice, branch_id) -> bool:
    """Whether any share of ``invoice`` sits at a branch other than ``branch_id``."""
    from .models import DeferredIncomeEntry

    if invoice is None or branch_id is None:
        return False
    return (DeferredIncomeEntry.objects.filter(invoice=invoice, branch__isnull=False)
            .exclude(branch_id=branch_id).exists())


def plan_released_takeback(invoice, amount, *, branch_id) -> list[UnwindStep]:
    """The released shares an adjustment of ``branch_id`` takes back past every waiting one.

    Asked only for a bill a receivable move has carried between branches
    (:func:`vs_finance.bill_adjustments.plan_adjustment`). On any other bill the
    rest of an adjustment is its own branch's revenue or allowance, as it has
    always been.

    On such a bill the revenue each share released belongs to the branch that
    released it. Tunde's January share was released at Ikeja after his term
    moved to Lekki; a credit note at Lekki cancelling the whole term takes that
    January revenue back from Ikeja, exactly as it would have taken the share
    back from Ikeja's deferred income had the release not run yet, so who bears
    the cancellation never depends on when the release ran.

    Latest months first, as :func:`plan_unwind`. A share gives at most what it
    released less what adjustments already took back after release, and every
    share taken is recorded (:class:`~vs_finance.models.DeferredIncomeUnwind`),
    at the adjusting branch too, so the order holds across several adjustments.
    """
    from .models import DeferredIncomeEntry, DeferredIncomeUnwind

    remaining = int(amount or 0)
    if remaining <= 0 or invoice is None or branch_id is None:
        return []
    released = list(
        DeferredIncomeEntry.objects.select_for_update()
        .filter(invoice=invoice, status=DeferredIncomeStatus.RELEASED)
        .order_by("-recognition_date", "-id")
    )
    taken = dict(
        DeferredIncomeUnwind.objects
        .filter(entry__in=released, after_release=True, restored=False)
        .values("entry_id").annotate(total=Sum("amount")).values_list("entry_id", "total")
    )
    plan = []
    for entry in released:
        if remaining <= 0:
            break
        take = min(int(entry.released_amount) - int(taken.get(entry.pk) or 0), remaining)
        if take > 0:
            plan.append(UnwindStep(entry, take, after_release=True))
            remaining -= take
    return plan


def apply_unwind(plan, *, adjustment_entry) -> int:
    """Record ``plan`` against the posted adjusting journal. Returns the kobo taken.

    Every waiting share taken is reduced, and every share taken gets its
    :class:`~vs_finance.models.DeferredIncomeUnwind` row, so voiding the
    adjustment restores it (:func:`restore_unwinds`). Which branch's books the
    debits came out of is decided before the journal posts
    (:mod:`vs_finance.bill_adjustments`).
    """
    from .models import DeferredIncomeUnwind

    for step in plan:
        entry = step.entry
        if not step.after_release:
            entry.unwound_amount += step.take
            if entry.open_amount == 0:
                entry.status = DeferredIncomeStatus.CANCELLED
            entry.save(update_fields=["unwound_amount", "status", "updated_at"])
        DeferredIncomeUnwind.objects.create(
            entry=entry, adjustment_entry=adjustment_entry, amount=step.take,
            after_release=step.after_release,
        )
    return sum(step.take for step in plan)


def restore_unwinds(adjustment_entry, *, actor_user=None, date=None) -> int:
    """Give back what a voided adjustment took from the schedule. Returns the kobo restored.

    A share still waiting gets its amount back. A share released since (it was
    only partly taken back, and the rest was released) cannot reopen, so the
    restored amount becomes a new share of its own on the same date and branch,
    released with the next run. A share taken back after its release keeps its
    figures; the void's own reversal puts the revenue back.

    Each branch's side of income given back
    (:func:`vs_finance.bill_adjustments.give_back`) is voided with the adjustment,
    dated ``date``, so the branch that booked the income has it again and the
    inter-branch pair returns to what it was.
    """
    from .models import DeferredIncomeEntry, DeferredIncomeUnwind, InterBranchTransfer

    restored = 0
    for unwind in (DeferredIncomeUnwind.objects.select_for_update()
                   .filter(adjustment_entry=adjustment_entry, restored=False)
                   .select_related("entry").order_by("pk")):
        entry = unwind.entry
        if unwind.after_release:
            pass
        elif entry.status == DeferredIncomeStatus.RELEASED:
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
        if not unwind.after_release:
            restored += int(unwind.amount)
    given_back = list(InterBranchTransfer.objects.filter(
        adjustment_entry=adjustment_entry, status=DocumentStatus.POSTED,
    ).order_by("pk"))
    if given_back:
        from .inter_branch import _void_transfer_atomic

        for transfer in given_back:
            _void_transfer_atomic(
                transfer, actor_user=actor_user, date=date, adjustment_entry=adjustment_entry,
            )
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
def release_deferred_income(entity, *, up_to, actor_user=None, allow_restricted=False,
                            branch=None):
    """Move every share recognised on or before ``up_to`` to revenue. Returns the releases.

    One journal per branch per month: ``Dr deferred income``, ``Cr revenue`` per
    revenue account and cost centre, each naming its branch (a share keeps its
    invoice's). Idempotent: only waiting shares are released, so a second run the
    same day posts nothing. ``allow_restricted`` lets the period-close step post
    into a soft-closed month, as depreciation does.
    """
    from .models import DeferredIncomeEntry, DeferredIncomeRelease, JournalEntry, JournalLine
    from .posting import post_journal, resolve_period

    pending = DeferredIncomeEntry.objects.select_for_update().filter(
        entity=entity, status=DeferredIncomeStatus.PENDING,
        recognition_date__lte=up_to, amount__gt=F("unwound_amount"),
    )
    if branch is not None:
        pending = pending.filter(branch_id=getattr(branch, "pk", branch))
    entries = list(pending.order_by("recognition_date", "pk"))
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
            message=f"Released {format_naira(total)} of deferred income for {day:%B %Y}.",
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
            message=f"Reversed a deferred income release of {format_naira(release.amount)}.",
            journal_id=release.journal_id, reversal_id=reversal.pk,
        )
    return len(releases)


def sealed_release_branches(entity, periods) -> dict:
    """``{period pk: [branch name, ...]}``: whose closed month seals each period's undo.

    :func:`reverse_deferred_release` undoes every branch's release of a month at
    once, and a branch that has closed that month on its own keeps its release
    sealed with it: the reversing journal would post into that branch's closed
    month, which the posting guard refuses. So one such branch is enough to refuse
    the whole month's undo, including the releases of branches still open. This
    names those branches, for each of ``periods`` that has any, so a list can say
    which rows the undo would refuse and why before anybody tries it.

    The answer is the entity's, not a reader's: the undo is a whole-tenant run, so
    a branch the reader cannot see still decides it. Two queries for any number of
    periods.
    """
    from .models import BranchFiscalPeriod, DeferredIncomeRelease

    periods = {period.pk: period for period in periods if period is not None}
    if not periods:
        return {}
    closed = defaultdict(dict)
    for period_id, branch_id, name in (
        BranchFiscalPeriod.objects.filter(period_id__in=periods)
        .exclude(status=PeriodStatus.OPEN)
        .values_list("period_id", "branch_id", "branch__name")
    ):
        closed[period_id][branch_id] = name
    if not closed:
        return {}
    spans = [periods[pk] for pk in closed]
    sealed = defaultdict(set)
    for branch_id, day in (
        DeferredIncomeRelease.objects.filter(
            entity=entity, reversed_at__isnull=True,
            branch_id__in={b for names in closed.values() for b in names},
            journal__date__gte=min(p.start_date for p in spans),
            journal__date__lte=max(p.end_date for p in spans),
        ).values_list("branch_id", "journal__date")
    ):
        for period in spans:
            if period.start_date <= day <= period.end_date and branch_id in closed[period.pk]:
                sealed[period.pk].add(closed[period.pk][branch_id])
    return {pk: sorted(names) for pk, names in sealed.items()}


def deferred_income_close_check(entity, period, branch=None):
    """Close check: no deferred income due in or before ``period`` is left unreleased.

    The close releases what is due before it checks, so the item carries
    ``close_settles`` for the preview to say so (:class:`vs_finance.close.ChecklistItem`).
    """
    from .close import ChecklistItem
    from .models import DeferredIncomeEntry
    from .money import format_naira

    due = DeferredIncomeEntry.objects.filter(
        entity=entity, status=DeferredIncomeStatus.PENDING,
        recognition_date__lte=period.end_date,
    ).filter(amount__gt=F("unwound_amount"))
    if branch is not None:
        due = due.filter(branch_id=getattr(branch, "pk", branch))
    row = due.aggregate(n=Count("pk"), total=Sum(F("amount") - F("unwound_amount")))
    count, total = int(row["n"] or 0), int(row["total"] or 0)
    shares = f"{count} deferred income {'share' if count == 1 else 'shares'} ({format_naira(total)})"
    are = "is" if count == 1 else "are"
    return ChecklistItem(
        name="deferred_income_released", passed=count == 0,
        detail=(
            "Every deferred income share due by the period's end is released." if not count
            else f"{shares} {are} due and not yet released."
        ),
        close_settles=(
            f"{shares} {are} due; closing the period releases {'it' if count == 1 else 'them'}."
            if count else ""
        ),
    )


deferred_income_close_check.check_name = "deferred_income_released"
deferred_income_close_check.supports_branch = True


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


def deferred_income_summary(entity, *, scope=None, rows=None) -> dict:
    """Deferred income per state for a reader, in kobo: waiting, released, next due.

    ``rows`` narrows the entity's shares further, as a ``?branch=`` filter does.
    """
    from .models import DeferredIncomeEntry

    if rows is None:
        rows = DeferredIncomeEntry.objects.all()
    rows = rows.filter(entity=entity)
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
