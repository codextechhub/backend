"""Fixed-asset services — acquisition and straight-line depreciation.

A capital asset is recognised at cost, then its cost (less salvage) is spread over its
useful life as an expense, period by period:

* **Acquire** (:func:`acquire_asset`): ``Dr PP&E, Cr bank/payable`` — capitalise the
  cost — then lay down the depreciation schedule.
* **Depreciate** (:func:`post_depreciation`): ``Dr depreciation expense, Cr accumulated
  depreciation`` for each due schedule row. The accumulated-depreciation contra-asset
  nets the PP&E down to net book value without touching the original cost.

Straight-line only: each month charges ``(cost − salvage) / useful_life_months``, with
the rounding remainder thrown onto the final period so the schedule sums to the
depreciable base **exactly** in integer kobo.
"""
from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    ACCUM_DEPRECIATION_CODE,
    AssetStatus,
    DEPRECIATION_EXPENSE_CODE,
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    PPE_ACCOUNT_CODE,
)
from .exceptions import DepreciationError, FinanceError
from .posting import post_journal, resolve_period


def _add_months(d: datetime.date, n: int) -> datetime.date:
    """Return ``d`` advanced by ``n`` whole months, clamping the day to month length."""
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp day (e.g. Jan 31 + 1 month -> Feb 28/29).
    if month == 12:
        next_month_first = datetime.date(year + 1, 1, 1)
    else:
        next_month_first = datetime.date(year, month + 1, 1)
    last_day = (next_month_first - datetime.timedelta(days=1)).day
    return datetime.date(year, month, min(d.day, last_day))


def _asset_accounts(asset):
    entity = asset.entity
    ppe = asset.asset_account or resolve_account(entity, PPE_ACCOUNT_CODE, label="PP&E")
    accum = asset.accumulated_depreciation_account or resolve_account(
        entity, ACCUM_DEPRECIATION_CODE, label="accumulated depreciation",
    )
    expense = asset.depreciation_expense_account or resolve_account(
        entity, DEPRECIATION_EXPENSE_CODE, label="depreciation expense",
    )
    return ppe, accum, expense


@transaction.atomic
def build_depreciation_schedule(asset):
    """(Re)build the straight-line :class:`DepreciationSchedule` for ``asset``.

    Rebuilds only while no row has posted yet (so a part-depreciated asset is never
    silently re-planned). Returns the list of schedule rows. The last row absorbs the
    integer-rounding remainder so the rows sum to ``depreciable_base`` exactly.
    """
    from .models import DepreciationSchedule

    if asset.schedule.filter(is_posted=True).exists():
        raise DepreciationError(
            "Cannot rebuild a schedule once depreciation has started posting.",
        )
    asset.schedule.all().delete()

    months = asset.useful_life_months
    if months <= 0:
        raise DepreciationError("Useful life must be a positive number of months.")

    base = asset.depreciable_base
    per_month = base // months
    remainder = base - per_month * months  # piled onto the final month

    rows = []
    # First charge in the month following acquisition.
    for seq in range(1, months + 1):
        amount = per_month + (remainder if seq == months else 0)
        rows.append(DepreciationSchedule(
            asset=asset, seq=seq,
            depreciation_date=_add_months(asset.acquisition_date, seq),
            amount=amount,
        ))
    DepreciationSchedule.objects.bulk_create(rows)
    return list(asset.schedule.all())


def acquire_asset(asset, *, credit_account=None, bank_account=None, actor_user=None,
                  build_schedule=True):
    """Capitalise an asset (``Dr PP&E, Cr bank/payable``) and lay down its schedule.

    Provide either ``bank_account`` (its GL cash account is credited) or an explicit
    ``credit_account`` (e.g. an AP/loan account). Records a durable rejection audit on
    any :class:`FinanceError`, then re-raises.
    """
    try:
        return _acquire_asset_atomic(
            asset, credit_account=credit_account, bank_account=bank_account,
            actor_user=actor_user, build_schedule=build_schedule,
        )
    except FinanceError as exc:
        record_rejection(
            entity=asset.entity, action=FinanceAuditAction.ASSET_ACQUIRED,
            exc=exc, actor_user=actor_user, target=asset,
        )
        raise


@transaction.atomic
def _acquire_asset_atomic(asset, *, credit_account=None, bank_account=None,
                          actor_user=None, build_schedule=True):
    from .models import JournalEntry, JournalLine

    if asset.asset_status != AssetStatus.DRAFT:
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} is '{asset.asset_status}', "
            f"only a draft can be acquired.",
        )
    if asset.cost <= 0:
        raise DepreciationError("An asset must have a positive cost.")

    ppe, accum, expense = _asset_accounts(asset)
    credit = credit_account or (bank_account.gl_account if bank_account else None)
    if credit is None:
        raise DepreciationError("Provide a bank_account or credit_account to fund the acquisition.")

    period = resolve_period(asset.entity, asset.acquisition_date)
    entry = JournalEntry.objects.create(
        entity=asset.entity, branch=asset.branch,
        date=asset.acquisition_date, period=period, source=JournalSource.PURCHASE,
        narration=f"Acquire {asset.name}", created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=ppe, debit=asset.cost, credit=0,
        description=asset.name, line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=credit, debit=0, credit=asset.cost,
        description=f"Acquire {asset.name}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    asset.acquisition_journal = entry
    asset.asset_account = ppe
    asset.accumulated_depreciation_account = accum
    asset.depreciation_expense_account = expense
    asset.asset_status = AssetStatus.ACTIVE
    asset.status = DocumentStatus.POSTED
    asset.save(update_fields=[
        "acquisition_journal", "asset_account", "accumulated_depreciation_account",
        "depreciation_expense_account", "asset_status", "status", "updated_at",
    ])

    if build_schedule:
        build_depreciation_schedule(asset)

    record(
        entity=asset.entity, action=FinanceAuditAction.ASSET_ACQUIRED,
        actor_user=actor_user, target=asset,
        message=f"Capitalised {asset.name} ({asset.cost} kobo).",
        journal_id=entry.pk, cost=asset.cost,
    )
    return asset


def post_depreciation(asset, *, up_to_date, actor_user=None, allow_restricted=False):
    """Post every unposted schedule row dated on/before ``up_to_date``.

    Each row raises ``Dr depreciation expense, Cr accumulated depreciation`` and updates
    the asset's running ``accumulated_depreciation``; the asset flips to
    ``FULLY_DEPRECIATED`` once the last row posts. ``allow_restricted`` lets the
    period-close process post into a soft-closed period. Returns the rows posted.
    """
    try:
        return _post_depreciation_atomic(
            asset, up_to_date=up_to_date, actor_user=actor_user,
            allow_restricted=allow_restricted,
        )
    except FinanceError as exc:
        record_rejection(
            entity=asset.entity, action=FinanceAuditAction.DEPRECIATION_POSTED,
            exc=exc, actor_user=actor_user, target=asset,
        )
        raise


@transaction.atomic
def _post_depreciation_atomic(asset, *, up_to_date, actor_user=None, allow_restricted=False):
    from .models import JournalEntry, JournalLine

    if asset.asset_status not in (AssetStatus.ACTIVE, AssetStatus.DRAFT):
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} is '{asset.asset_status}'; "
            f"depreciation cannot be posted.",
        )

    _, accum, expense = _asset_accounts(asset)
    due = list(
        asset.schedule.filter(is_posted=False, depreciation_date__lte=up_to_date)
        .order_by("seq")
    )
    posted = []
    for row in due:
        if row.amount <= 0:
            row.is_posted = True
            row.posted_at = timezone.now()
            row.save(update_fields=["is_posted", "posted_at", "updated_at"])
            continue
        period = resolve_period(asset.entity, row.depreciation_date)
        entry = JournalEntry.objects.create(
            entity=asset.entity, branch=asset.branch,
            date=row.depreciation_date, period=period, source=JournalSource.CLOSING,
            narration=f"Depreciation {asset.name} #{row.seq}", created_by=actor_user,
        )
        JournalLine.objects.create(
            entry=entry, account=expense, debit=row.amount, credit=0,
            description="Depreciation", line_no=1,
        )
        JournalLine.objects.create(
            entry=entry, account=accum, debit=0, credit=row.amount,
            description="Accumulated depreciation", line_no=2,
        )
        post_journal(entry, actor_user=actor_user, allow_restricted=allow_restricted)

        row.is_posted = True
        row.journal = entry
        row.posted_at = timezone.now()
        row.save(update_fields=["is_posted", "journal", "posted_at", "updated_at"])
        asset.accumulated_depreciation += row.amount
        posted.append(row)

    if not asset.schedule.filter(is_posted=False).exists():
        asset.asset_status = AssetStatus.FULLY_DEPRECIATED
    asset.save(update_fields=["accumulated_depreciation", "asset_status", "updated_at"])

    if posted:
        record(
            entity=asset.entity, action=FinanceAuditAction.DEPRECIATION_POSTED,
            actor_user=actor_user, target=asset,
            message=f"Posted {len(posted)} depreciation charge(s) for {asset.name}.",
            charges=len(posted),
            total=sum(r.amount for r in posted),
            accumulated=asset.accumulated_depreciation,
        )
    return posted
