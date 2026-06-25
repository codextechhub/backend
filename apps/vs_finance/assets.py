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
    DepreciationMethod,
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


def _straight_line_amounts(base: int, months: int) -> list[int]:
    """Equal monthly charges; the final month absorbs the rounding remainder."""
    per_month = base // months
    remainder = base - per_month * months
    return [per_month + (remainder if seq == months else 0) for seq in range(1, months + 1)]


def _declining_balance_amounts(cost: int, salvage: int, months: int) -> list[int]:
    """Double-declining balance with a switch to straight-line of the remaining base.

    Each month charges the greater of the declining-balance rate (``2 / months`` of the
    opening book value) and straight-lining the remaining depreciable amount over the
    months left — the textbook switch that still lands exactly on ``salvage``. Charges
    never take book value below salvage; the final month absorbs the remainder.
    """
    base = cost - salvage
    amounts: list[int] = []
    book_value = cost
    for seq in range(1, months + 1):
        remaining_months = months - seq + 1
        if seq == months:
            charge = book_value - salvage  # land exactly on salvage
        else:
            declining = book_value * 2 // months
            straight = (book_value - salvage) // remaining_months
            charge = max(declining, straight)
            charge = min(charge, book_value - salvage)  # never below salvage
        charge = max(charge, 0)
        amounts.append(charge)
        book_value -= charge
    # Guard: rounding can leave a kobo or two — pile any remainder on the last charge.
    drift = base - sum(amounts)
    if drift and amounts:
        amounts[-1] += drift
    return amounts


@transaction.atomic
def build_depreciation_schedule(asset):
    """(Re)build the :class:`DepreciationSchedule` for ``asset`` (straight-line or
    declining-balance, per ``asset.method``).

    Rebuilds only while no row has posted yet (so a part-depreciated asset is never
    silently re-planned). Returns the list of schedule rows. The rows always sum to
    ``depreciable_base`` exactly (the final month absorbs the rounding remainder).
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

    if asset.method == DepreciationMethod.DECLINING_BALANCE:
        amounts = _declining_balance_amounts(asset.cost, asset.salvage_value, months)
    else:
        amounts = _straight_line_amounts(asset.depreciable_base, months)

    rows = [
        DepreciationSchedule(
            asset=asset, seq=seq,
            depreciation_date=_add_months(asset.acquisition_date, seq),
            amount=amount,
        )
        for seq, amount in enumerate(amounts, start=1)
    ]
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


def _due_depreciation(entity, up_to_date):
    """The unposted, due schedule rows across the entity's active assets."""
    from .models import DepreciationSchedule

    return list(
        DepreciationSchedule.objects
        .filter(
            asset__entity=entity, is_posted=False, depreciation_date__lte=up_to_date,
            asset__asset_status__in=(AssetStatus.ACTIVE, AssetStatus.DRAFT),
        )
        .select_related("asset", "asset__depreciation_expense_account",
                        "asset__accumulated_depreciation_account")
        .order_by("asset_id", "seq")
    )


def preview_period_depreciation(entity, *, up_to_date):
    """Summarise depreciation due up to ``up_to_date``, grouped by expense/accum account.

    The Run-depreciation preview: one compound journal will Dr each expense account and
    Cr each accumulated-depreciation account by the totals here.
    """
    expense, accum = {}, {}
    asset_ids, total = set(), 0
    for row in _due_depreciation(entity, up_to_date):
        if row.amount <= 0:
            continue
        _, accum_acct, expense_acct = _asset_accounts(row.asset)
        expense[expense_acct] = expense.get(expense_acct, 0) + row.amount
        accum[accum_acct] = accum.get(accum_acct, 0) + row.amount
        asset_ids.add(row.asset_id)
        total += row.amount
    debits = [{"account": a.code, "name": a.name, "amount": v} for a, v in expense.items()]
    credits = [{"account": a.code, "name": a.name, "amount": v} for a, v in accum.items()]
    return {"debits": debits, "credits": credits, "total": total,
            "asset_count": len(asset_ids)}


def run_period_depreciation(entity, *, up_to_date, actor_user=None):
    """Post one compound journal for every depreciation charge due up to ``up_to_date``."""
    try:
        return _run_period_depreciation_atomic(entity, up_to_date=up_to_date, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=entity, action=FinanceAuditAction.DEPRECIATION_POSTED,
            exc=exc, actor_user=actor_user, target=None,
        )
        raise


@transaction.atomic
def _run_period_depreciation_atomic(entity, *, up_to_date, actor_user=None):
    from .models import JournalEntry, JournalLine

    rows = _due_depreciation(entity, up_to_date)
    charges = [r for r in rows if r.amount > 0]
    if not charges:
        raise DepreciationError("No depreciation is due up to that date.")

    expense, accum = {}, {}
    for row in charges:
        _, accum_acct, expense_acct = _asset_accounts(row.asset)
        expense[expense_acct] = expense.get(expense_acct, 0) + row.amount
        accum[accum_acct] = accum.get(accum_acct, 0) + row.amount

    period = resolve_period(entity, up_to_date)
    entry = JournalEntry.objects.create(
        entity=entity, date=up_to_date, period=period, source=JournalSource.CLOSING,
        narration=f"Depreciation run to {up_to_date}", created_by=actor_user,
    )
    line_no = 0
    for acct, amount in expense.items():
        line_no += 1
        JournalLine.objects.create(entry=entry, account=acct, debit=amount, credit=0,
                                   description="Depreciation", line_no=line_no)
    for acct, amount in accum.items():
        line_no += 1
        JournalLine.objects.create(entry=entry, account=acct, debit=0, credit=amount,
                                   description="Accumulated depreciation", line_no=line_no)
    post_journal(entry, actor_user=actor_user)

    # Mark every due row posted (zero-amount rows too) and roll up per asset.
    by_asset: dict = {}
    for row in rows:
        row.is_posted = True
        row.journal = entry if row.amount > 0 else None
        row.posted_at = timezone.now()
        row.save(update_fields=["is_posted", "journal", "posted_at", "updated_at"])
        by_asset[row.asset] = by_asset.get(row.asset, 0) + row.amount
    for asset, amount in by_asset.items():
        asset.accumulated_depreciation += amount
        if not asset.schedule.filter(is_posted=False).exists():
            asset.asset_status = AssetStatus.FULLY_DEPRECIATED
        asset.save(update_fields=["accumulated_depreciation", "asset_status", "updated_at"])

    total = sum(r.amount for r in charges)
    record(
        entity=entity, action=FinanceAuditAction.DEPRECIATION_POSTED,
        actor_user=actor_user, target=entry,
        message=f"Posted a {total} kobo depreciation run across {len(by_asset)} asset(s).",
        journal_id=entry.pk, charges=len(charges), total=total, assets=len(by_asset),
    )
    return {"journal_id": entry.pk, "total": total, "charge_count": len(charges),
            "asset_count": len(by_asset)}


def dispose_asset(asset, *, disposal_date, proceeds=0, bank_account=None,
                  gain_loss_account=None, actor_user=None):
    """Retire/sell an asset: clear its cost + accumulated depreciation, book proceeds and
    the gain/loss. ``Dr accum dep, Dr bank (proceeds), Dr loss | Cr gain, Cr asset cost``."""
    try:
        return _dispose_asset_atomic(
            asset, disposal_date=disposal_date, proceeds=proceeds,
            bank_account=bank_account, gain_loss_account=gain_loss_account, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=asset.entity, action=FinanceAuditAction.ASSET_DISPOSED,
            exc=exc, actor_user=actor_user, target=asset,
        )
        raise


@transaction.atomic
def _dispose_asset_atomic(asset, *, disposal_date, proceeds=0, bank_account=None,
                          gain_loss_account=None, actor_user=None):
    from .models import JournalEntry, JournalLine

    if asset.asset_status not in (AssetStatus.ACTIVE, AssetStatus.FULLY_DEPRECIATED):
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} is '{asset.asset_status}'; "
            f"only an active or fully-depreciated asset can be disposed.",
        )
    proceeds = int(proceeds or 0)
    nbv = asset.cost - asset.accumulated_depreciation
    gain_loss = proceeds - nbv  # >0 gain, <0 loss
    if proceeds > 0 and bank_account is None:
        raise DepreciationError("A bank account is required to record disposal proceeds.")
    if gain_loss != 0 and gain_loss_account is None:
        raise DepreciationError("A gain/loss account is required to record the disposal result.")

    ppe, accum, _ = _asset_accounts(asset)
    period = resolve_period(asset.entity, disposal_date)
    entry = JournalEntry.objects.create(
        entity=asset.entity, branch=asset.branch, date=disposal_date,
        period=period, source=JournalSource.BANK,
        narration=f"Disposal of {asset.name}", created_by=actor_user,
    )
    line_no = 0
    if asset.accumulated_depreciation > 0:
        line_no += 1
        JournalLine.objects.create(entry=entry, account=accum, debit=asset.accumulated_depreciation,
                                   credit=0, description="Accumulated depreciation written back", line_no=line_no)
    if proceeds > 0:
        line_no += 1
        JournalLine.objects.create(entry=entry, account=bank_account.gl_account, debit=proceeds,
                                   credit=0, description="Disposal proceeds", line_no=line_no)
    if gain_loss < 0:
        line_no += 1
        JournalLine.objects.create(entry=entry, account=gain_loss_account, debit=-gain_loss,
                                   credit=0, description="Loss on disposal", line_no=line_no)
    elif gain_loss > 0:
        line_no += 1
        JournalLine.objects.create(entry=entry, account=gain_loss_account, debit=0,
                                   credit=gain_loss, description="Gain on disposal", line_no=line_no)
    line_no += 1
    JournalLine.objects.create(entry=entry, account=ppe, debit=0, credit=asset.cost,
                               description="Asset cost removed", line_no=line_no)
    post_journal(entry, actor_user=actor_user)

    asset.disposal_journal = entry
    asset.disposal_date = disposal_date
    asset.asset_status = AssetStatus.DISPOSED
    asset.save(update_fields=["disposal_journal", "disposal_date", "asset_status", "updated_at"])

    record(
        entity=asset.entity, action=FinanceAuditAction.ASSET_DISPOSED,
        actor_user=actor_user, target=asset,
        message=f"Disposed {asset.name}: proceeds {proceeds}, {'gain' if gain_loss >= 0 else 'loss'} {abs(gain_loss)} kobo.",
        journal_id=entry.pk, proceeds=proceeds, gain_loss=gain_loss, nbv=nbv,
    )
    return entry
