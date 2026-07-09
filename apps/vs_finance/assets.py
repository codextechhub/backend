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


# Advance a date by whole months.
def _add_months(d: datetime.date, n: int) -> datetime.date:
    """Return ``d`` advanced by ``n`` whole months, clamping the day to month length."""
    month_index = d.month - 1 + n  # Convert to zero-based month index and apply offset.
    year = d.year + month_index // 12  # Carry overflow months into the year.
    month = month_index % 12 + 1  # Convert zero-based month back to 1-12.
    # Clamp day (e.g. Jan 31 + 1 month -> Feb 28/29).  # Avoid invalid month-end dates.
    if month == 12:  # December's next month is January of next year.
        next_month_first = datetime.date(year + 1, 1, 1)
    else:  # Any other month advances within the same year.
        next_month_first = datetime.date(year, month + 1, 1)
    last_day = (next_month_first - datetime.timedelta(days=1)).day
    return datetime.date(year, month, min(d.day, last_day))


# Resolve the three GL accounts used by asset accounting.
def _asset_accounts(asset):
    entity = asset.entity  # Asset entity determines account lookup scope.
    ppe = asset.asset_account or resolve_account(entity, PPE_ACCOUNT_CODE, label="PP&E")  # Asset cost account.
    accum = asset.accumulated_depreciation_account or resolve_account(  # Contra-asset accumulated depreciation account.
        entity, ACCUM_DEPRECIATION_CODE, label="accumulated depreciation",  # Resolve default accumulated depreciation account.
    )
    expense = asset.depreciation_expense_account or resolve_account(  # Depreciation expense account.
        entity, DEPRECIATION_EXPENSE_CODE, label="depreciation expense",  # Resolve default depreciation expense account.
    )
    return ppe, accum, expense  # Return cost, contra-asset, and expense accounts.


# Build exact straight-line monthly charges.
def _straight_line_amounts(base: int, months: int) -> list[int]:
    """Equal monthly charges; the final month absorbs the rounding remainder."""
    per_month = base // months  # Integer division gives the common monthly charge.
    remainder = base - per_month * months  # Remainder kobo that cannot be spread evenly.
    return [per_month + (remainder if seq == months else 0) for seq in range(1, months + 1)]  # Add remainder to final month.


# Build double-declining schedule.
def _declining_balance_amounts(cost: int, salvage: int, months: int) -> list[int]:
    """Double-declining balance with a switch to straight-line of the remaining base.

    Each month charges the greater of the declining-balance rate (``2 / months`` of the
    opening book value) and straight-lining the remaining depreciable amount over the
    months left — the textbook switch that still lands exactly on ``salvage``. Charges
    never take book value below salvage; the final month absorbs the remainder.
    """
    base = cost - salvage  # Total depreciable amount.
    amounts: list[int] = []  # Monthly charge list.
    book_value = cost  # Opening book value before each charge.
    for seq in range(1, months + 1):  # Build one charge per useful-life month.
        remaining_months = months - seq + 1  # Months left including this one.
        if seq == months:  # Final month must land exactly at salvage value.
            charge = book_value - salvage  # land exactly on salvage
        else:  # Earlier months use double-declining with straight-line switch.
            declining = book_value * 2 // months  # Double-declining charge using integer kobo.
            straight = (book_value - salvage) // remaining_months  # Straight-line remaining base.
            charge = max(declining, straight)  # Switch when straight-line catches up.
            charge = min(charge, book_value - salvage)  # never below salvage
        charge = max(charge, 0)  # Prevent negative charges when salvage exceeds book value.
        amounts.append(charge)  # Store this month's depreciation.
        book_value -= charge  # Reduce book value for next month.
    # Guard: rounding can leave a kobo or two — pile any remainder on the last charge.  # Keep total exact.
    drift = base - sum(amounts)  # Difference between intended base and generated charges.
    if drift and amounts:  # Adjust only when there is a schedule to adjust.
        amounts[-1] += drift  # Put any rounding drift on final charge.
    return amounts  # Return exact monthly charges.


@transaction.atomic
# Build or rebuild the asset depreciation schedule.
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

    months = asset.useful_life_months  # Useful life determines number of monthly rows.
    if months <= 0:  # A schedule needs at least one month.
        raise DepreciationError("Useful life must be a positive number of months.")

    if asset.method == DepreciationMethod.DECLINING_BALANCE:  # Use declining balance when selected.
        amounts = _declining_balance_amounts(asset.cost, asset.salvage_value, months)  # Build declining schedule.
    else:  # Straight-line is the default method.
        amounts = _straight_line_amounts(asset.depreciable_base, months)  # Build straight-line schedule.

    rows = [  # Prepare schedule rows for bulk insertion.
        DepreciationSchedule(  # One monthly depreciation row.
            asset=asset, seq=seq,  # Link asset and sequence number.
            depreciation_date=_add_months(asset.acquisition_date, seq),  # Date each charge one month apart.
            amount=amount,  # Monthly charge in kobo.
        )
        for seq, amount in enumerate(amounts, start=1)  # Pair sequence numbers with amounts.
    ]
    DepreciationSchedule.objects.bulk_create(rows)
    return list(asset.schedule.all())  # Return persisted schedule rows.


# Handle the acquire asset workflow.
def acquire_asset(asset, *, credit_account=None, bank_account=None, actor_user=None,
                  build_schedule=True):  # Public wrapper for asset acquisition posting.
    """Capitalise an asset (``Dr PP&E, Cr bank/payable``) and lay down its schedule.

    Provide either ``bank_account`` (its GL cash account is credited) or an explicit
    ``credit_account`` (e.g. an AP/loan account). Records a durable rejection audit on
    any :class:`FinanceError`, then re-raises.
    """
    try:  # Atomic worker performs accounting mutation.
        return _acquire_asset_atomic(  # Capitalize asset and optionally build schedule.
            asset, credit_account=credit_account, bank_account=bank_account,  # Funding source options.
            actor_user=actor_user, build_schedule=build_schedule,  # Actor and schedule toggle.
        )
    except FinanceError as exc:  # Failed acquisitions should be auditable.
        record_rejection(  # Record durable rejection outside rolled-back posting work.
            entity=asset.entity, action=FinanceAuditAction.ASSET_ACQUIRED,  # Existing asset acquisition audit action.
            exc=exc, actor_user=actor_user, target=asset,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Support the acquire asset atomic workflow.
def _acquire_asset_atomic(asset, *, credit_account=None, bank_account=None,
                          actor_user=None, build_schedule=True):  # Transactional asset acquisition implementation.
    from .models import JournalEntry, JournalLine

    if asset.asset_status != AssetStatus.DRAFT:  # Only draft assets can be acquired.
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} is '{asset.asset_status}', "
            f"only a draft can be acquired.",
        )
    if asset.cost <= 0:  # Capitalized assets must have positive cost.
        raise DepreciationError("An asset must have a positive cost.")

    ppe, accum, expense = _asset_accounts(asset)  # Resolve asset cost, contra, and expense accounts.
    credit = credit_account or (bank_account.gl_account if bank_account else None)  # Choose funding account.
    if credit is None:  # Acquisition journal needs a credit side.
        raise DepreciationError("Provide a bank_account or credit_account to fund the acquisition.")

    period = resolve_period(asset.entity, asset.acquisition_date)  # Find acquisition period.
    entry = JournalEntry.objects.create(
        entity=asset.entity, branch=asset.branch,  # Scope entity and optional branch.
        date=asset.acquisition_date, period=period, source=JournalSource.PURCHASE,  # Acquisition date/period/source.
        narration=f"Acquire {asset.name}", created_by=actor_user,  # Journal narration and actor.
    )
    JournalLine.objects.create(
        entry=entry, account=ppe, debit=asset.cost, credit=0,  # Dr asset cost account.
        description=asset.name, line_no=1,  # Line description and order.
    )
    JournalLine.objects.create(
        entry=entry, account=credit, debit=0, credit=asset.cost,  # Cr bank/payable/source account.
        description=f"Acquire {asset.name}", line_no=2,  # Line description and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post acquisition journal.

    asset.acquisition_journal = entry  # Link asset to acquisition journal.
    asset.asset_account = ppe  # Persist cost account used.
    asset.accumulated_depreciation_account = accum  # Persist contra account used.
    asset.depreciation_expense_account = expense  # Persist expense account used.
    asset.asset_status = AssetStatus.ACTIVE  # Asset is now active.
    asset.status = DocumentStatus.POSTED  # Finance document lifecycle is posted.
    asset.save(update_fields=[
        "acquisition_journal", "asset_account", "accumulated_depreciation_account",  # Journal and balance-sheet accounts.
        "depreciation_expense_account", "asset_status", "status", "updated_at",  # Expense account and statuses.
    ])

    if build_schedule:  # Caller can skip schedule creation when needed.
        build_depreciation_schedule(asset)  # Lay down depreciation schedule.

    record(  # Audit successful acquisition.
        entity=asset.entity, action=FinanceAuditAction.ASSET_ACQUIRED,  # Audit action.
        actor_user=actor_user, target=asset,  # Actor and target context.
        message=f"Capitalised {asset.name} ({asset.cost} kobo).",  # Human-readable audit message.
        journal_id=entry.pk, cost=asset.cost,  # Structured audit metadata.
    )
    return asset  # Return acquired asset.


# Public wrapper for per-asset depreciation.
def post_depreciation(asset, *, up_to_date, actor_user=None, allow_restricted=False):
    """Post every unposted schedule row dated on/before ``up_to_date``.

    Each row raises ``Dr depreciation expense, Cr accumulated depreciation`` and updates
    the asset's running ``accumulated_depreciation``; the asset flips to
    ``FULLY_DEPRECIATED`` once the last row posts. ``allow_restricted`` lets the
    period-close process post into a soft-closed period. Returns the rows posted.
    """
    try:  # Atomic worker posts due schedule rows.
        return _post_depreciation_atomic(  # Post depreciation for one asset.
            asset, up_to_date=up_to_date, actor_user=actor_user,  # Cutoff date and actor.
            allow_restricted=allow_restricted,  # Period-close privilege flag.
        )
    except FinanceError as exc:  # Failed depreciation should be auditable.
        record_rejection(  # Record durable rejection.
            entity=asset.entity, action=FinanceAuditAction.DEPRECIATION_POSTED,  # Existing depreciation audit action.
            exc=exc, actor_user=actor_user, target=asset,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Transactional per-asset depreciation.
def _post_depreciation_atomic(asset, *, up_to_date, actor_user=None, allow_restricted=False):
    from .models import JournalEntry, JournalLine

    if asset.asset_status != AssetStatus.ACTIVE:  # Only active assets can depreciate.
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} is '{asset.asset_status}'; "
            f"only an ACTIVE asset can be depreciated.",
        )

    _, accum, expense = _asset_accounts(asset)  # Resolve contra and expense accounts.
    due = list(  # Load due unposted schedule rows.
        asset.schedule.filter(is_posted=False, depreciation_date__lte=up_to_date)
        .order_by("seq")
    )
    posted = []  # Non-zero rows posted with journals.
    for row in due:  # Process each due schedule row.
        if row.amount <= 0:  # Zero rows need no journal.
            row.is_posted = True  # Mark schedule row complete.
            row.posted_at = timezone.now()
            row.save(update_fields=["is_posted", "posted_at", "updated_at"])
            continue
        period = resolve_period(asset.entity, row.depreciation_date)  # Find depreciation period.
        entry = JournalEntry.objects.create(
            entity=asset.entity, branch=asset.branch,  # Scope entity and optional branch.
            date=row.depreciation_date, period=period, source=JournalSource.CLOSING,  # Closing-source depreciation entry.
            narration=f"Depreciation {asset.name} #{row.seq}", created_by=actor_user,  # Narration and actor.
        )
        JournalLine.objects.create(
            entry=entry, account=expense, debit=row.amount, credit=0,  # Dr depreciation expense.
            description="Depreciation", line_no=1,  # Line label and order.
        )
        JournalLine.objects.create(
            entry=entry, account=accum, debit=0, credit=row.amount,  # Cr accumulated depreciation.
            description="Accumulated depreciation", line_no=2,  # Line label and order.
        )
        post_journal(entry, actor_user=actor_user, allow_restricted=allow_restricted)  # Post depreciation journal.

        row.is_posted = True  # Mark schedule row posted.
        row.journal = entry  # Link row to depreciation journal.
        row.posted_at = timezone.now()
        row.save(update_fields=["is_posted", "journal", "posted_at", "updated_at"])
        asset.accumulated_depreciation += row.amount  # Roll charge into asset accumulated depreciation.
        posted.append(row)  # Track non-zero posted row.

    if not asset.schedule.filter(is_posted=False).exists():
        asset.asset_status = AssetStatus.FULLY_DEPRECIATED  # Mark asset fully depreciated.
    asset.save(update_fields=["accumulated_depreciation", "asset_status", "updated_at"])

    if posted:  # Only audit when a non-zero journal was posted.
        record(  # Audit successful depreciation.
            entity=asset.entity, action=FinanceAuditAction.DEPRECIATION_POSTED,  # Audit action.
            actor_user=actor_user, target=asset,  # Actor and asset context.
            message=f"Posted {len(posted)} depreciation charge(s) for {asset.name}.",  # Human-readable audit message.
            charges=len(posted),  # Count of posted charges.
            total=sum(r.amount for r in posted),  # Total depreciation posted.
            accumulated=asset.accumulated_depreciation,  # New accumulated depreciation balance.
        )
    return posted  # Return non-zero schedule rows posted.


# Load all due unposted depreciation rows for an entity.
def _due_depreciation(entity, up_to_date):
    """The unposted, due schedule rows across the entity's active assets."""
    from .models import DepreciationSchedule

    return list(  # Materialize due rows for preview/posting.
        DepreciationSchedule.objects
        .filter(
            asset__entity=entity, is_posted=False, depreciation_date__lte=up_to_date,  # Entity, unposted, and due cutoff.
            asset__asset_status=AssetStatus.ACTIVE,  # Only active assets depreciate.
        )
        .select_related("asset", "asset__depreciation_expense_account",
                        "asset__accumulated_depreciation_account")  # Load contra-asset account.
        .order_by("asset_id", "seq")
    )


# Summarize due depreciation without posting.
def preview_period_depreciation(entity, *, up_to_date):
    """Summarise depreciation due up to ``up_to_date``, grouped by expense/accum account.

    The Run-depreciation preview: one compound journal will Dr each expense account and
    Cr each accumulated-depreciation account by the totals here.
    """
    expense, accum = {}, {}  # Group debit and credit totals by account object.
    asset_ids, total = set(), 0  # Track affected assets and total due amount.
    for row in _due_depreciation(entity, up_to_date):  # Walk due schedule rows.
        if row.amount <= 0:  # Zero rows do not affect preview journal totals.
            continue
        _, accum_acct, expense_acct = _asset_accounts(row.asset)  # Resolve posting accounts.
        expense[expense_acct] = expense.get(expense_acct, 0) + row.amount
        accum[accum_acct] = accum.get(accum_acct, 0) + row.amount
        asset_ids.add(row.asset_id)  # Track affected asset.
        total += row.amount  # Add to preview total.
    debits = [{"account": a.code, "name": a.name, "amount": v} for a, v in expense.items()]  # Shape debit preview rows.
    credits = [{"account": a.code, "name": a.name, "amount": v} for a, v in accum.items()]  # Shape credit preview rows.
    return {"debits": debits, "credits": credits, "total": total,  # Return compound journal preview.
            "asset_count": len(asset_ids)}  # Include affected asset count.


# Public wrapper for compound depreciation run.
def run_period_depreciation(entity, *, up_to_date, actor_user=None):
    """Post depreciation due up to ``up_to_date`` — one compound journal **per period**.

    Due charges are grouped by their :class:`FiscalPeriod` and each period gets its own
    compound journal (Dr per expense account / Cr per accumulated-depreciation account),
    dated at the latest ``depreciation_date`` in that period — so a charge never posts
    into the wrong period. If a period is CLOSED, :func:`post_journal` raises
    :class:`PeriodClosedError` and it propagates: the operator re-opens that period (via
    the period-reopen endpoint) and re-runs. Records a durable rejection audit on any
    :class:`FinanceError`.
    """
    try:  # Atomic worker performs all period-grouped postings.
        return _run_period_depreciation_atomic(entity, up_to_date=up_to_date, actor_user=actor_user)  # Run depreciation.
    except FinanceError as exc:  # Failed depreciation run should be auditable.
        record_rejection(  # Record durable rejection for the entity.
            entity=entity, action=FinanceAuditAction.DEPRECIATION_POSTED,  # Existing depreciation audit action.
            exc=exc, actor_user=actor_user, target=None,  # Error and actor context; no single target asset.
        )
        raise


@transaction.atomic
# Transactional compound depreciation run.
def _run_period_depreciation_atomic(entity, *, up_to_date, actor_user=None):
    from .models import JournalEntry, JournalLine

    rows = _due_depreciation(entity, up_to_date)  # Load due rows including zero-amount rows.
    charges = [r for r in rows if r.amount > 0]  # Non-zero rows need journal lines.
    if not charges:  # Nothing to post.
        raise DepreciationError("No depreciation is due up to that date.")

    # Group the charge rows by the fiscal period their depreciation_date falls in, so
    # each period is posted with its own compound journal dated within that period.  # Prevent cross-period postings.
    period_cache: dict = {}  # Cache period by depreciation date.
    groups: dict = {}  # period -> {"rows": [...], "latest_date": date}
    for row in charges:  # Assign each charge to its fiscal period.
        period = period_cache.get(row.depreciation_date)
        if period is None:  # Resolve uncached dates.
            period = resolve_period(entity, row.depreciation_date)  # Find period covering charge date.
            if period is None:  # Missing fiscal period is a configuration error.
                # Fail closed with a typed error rather than grouping under None.  # Avoid later AttributeError/invalid post.
                raise DepreciationError(
                    f"No fiscal period covers {row.depreciation_date}; create the "
                    f"fiscal year before running depreciation.",
                )
            period_cache[row.depreciation_date] = period  # Cache resolved period.
        bucket = groups.setdefault(period, {"rows": [], "latest_date": row.depreciation_date})  # Get period bucket.
        bucket["rows"].append(row)  # Add charge row to period bucket.
        if row.depreciation_date > bucket["latest_date"]:  # Keep latest charge date for journal date.
            bucket["latest_date"] = row.depreciation_date  # Update journal date.

    row_to_journal: dict = {}  # Map schedule row id to posted journal.
    journal_ids: list[int] = []  # Posted depreciation journal ids in chronological order.
    # Post chronologically so journal_ids[0] is the earliest period's journal.  # Stable audit metadata.
    for period in sorted(groups, key=lambda p: p.start_date):  # Process period buckets in date order.
        bucket = groups[period]  # Rows and latest date for this period.
        expense, accum = {}, {}  # Group debit and credit totals by account.
        for row in bucket["rows"]:  # Aggregate rows inside this period.
            _, accum_acct, expense_acct = _asset_accounts(row.asset)  # Resolve row posting accounts.
            expense[expense_acct] = expense.get(expense_acct, 0) + row.amount
            accum[accum_acct] = accum.get(accum_acct, 0) + row.amount

        entry = JournalEntry.objects.create(
            entity=entity, date=bucket["latest_date"], period=period,  # Entity, in-period date, and period.
            source=JournalSource.CLOSING,  # Depreciation run is a closing-source entry.
            narration=f"Depreciation run for {period.name}", created_by=actor_user,  # Narration and actor.
        )
        line_no = 0  # Journal line counter.
        for acct, amount in expense.items():  # Emit grouped expense debit lines.
            line_no += 1  # Advance line order.
            JournalLine.objects.create(entry=entry, account=acct, debit=amount, credit=0,
                                       description="Depreciation", line_no=line_no)  # Dr depreciation expense.
        for acct, amount in accum.items():  # Emit grouped accumulated depreciation credit lines.
            line_no += 1  # Advance line order.
            JournalLine.objects.create(entry=entry, account=acct, debit=0, credit=amount,
                                       description="Accumulated depreciation", line_no=line_no)  # Cr accumulated depreciation.
        post_journal(entry, actor_user=actor_user)  # Validate and post compound journal.
        journal_ids.append(entry.pk)  # Track posted journal id.
        for row in bucket["rows"]:  # Link each row in the period to this journal.
            row_to_journal[row.pk] = entry  # Map schedule row to journal.

    # Mark every due row posted (zero-amount rows too) and roll up per asset.  # Keep schedule state in sync.
    by_asset: dict = {}  # Accumulated depreciation amount by asset object.
    for row in rows:  # Update all due rows, including zero rows.
        row.is_posted = True  # Mark schedule row complete.
        row.journal = row_to_journal.get(row.pk)
        row.posted_at = timezone.now()
        row.save(update_fields=["is_posted", "journal", "posted_at", "updated_at"])
        by_asset[row.asset] = by_asset.get(row.asset, 0) + row.amount
    for asset, amount in by_asset.items():  # Update each affected asset.
        asset.accumulated_depreciation += amount  # Add posted depreciation.
        if not asset.schedule.filter(is_posted=False).exists():
            asset.asset_status = AssetStatus.FULLY_DEPRECIATED  # Mark asset fully depreciated.
        asset.save(update_fields=["accumulated_depreciation", "asset_status", "updated_at"])

    total = sum(r.amount for r in charges)  # Total non-zero depreciation posted.
    record(  # Audit the depreciation run.
        entity=entity, action=FinanceAuditAction.DEPRECIATION_POSTED,  # Audit action.
        actor_user=actor_user, target=None, target_type="LedgerEntity",  # Entity-level target.
        target_id=str(entity.pk),  # Structured target id.
        message=f"Posted a {total} kobo depreciation run across {len(by_asset)} asset(s) "  # Human-readable summary.
                f"in {len(journal_ids)} period(s).",  # Include period count.
        journal_id=journal_ids[0], journal_ids=journal_ids, charges=len(charges),  # Journal and charge metadata.
        total=total, assets=len(by_asset), period_count=len(journal_ids),  # Aggregate metadata.
    )
    return {"journal_id": journal_ids[0], "journal_ids": journal_ids,  # Return primary and all journal ids.
            "period_count": len(journal_ids), "total": total,  # Return period count and total.
            "charge_count": len(charges), "asset_count": len(by_asset)}  # Return row and asset counts.


# Handle the dispose asset workflow.
def dispose_asset(asset, *, disposal_date, proceeds=0, bank_account=None,
                  gain_loss_account=None, actor_user=None):  # Public wrapper for asset disposal.
    """Retire/sell an asset: clear its cost + accumulated depreciation, book proceeds and
    the gain/loss. ``Dr accum dep, Dr bank (proceeds), Dr loss | Cr gain, Cr asset cost``."""
    try:  # Atomic worker performs disposal accounting.
        return _dispose_asset_atomic(  # Dispose asset and post gain/loss journal.
            asset, disposal_date=disposal_date, proceeds=proceeds,  # Disposal date and proceeds.
            bank_account=bank_account, gain_loss_account=gain_loss_account, actor_user=actor_user,  # Accounts and actor.
        )
    except FinanceError as exc:  # Failed disposal should be auditable.
        record_rejection(  # Record durable rejection.
            entity=asset.entity, action=FinanceAuditAction.ASSET_DISPOSED,  # Audit action.
            exc=exc, actor_user=actor_user, target=asset,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Support the dispose asset atomic workflow.
def _dispose_asset_atomic(asset, *, disposal_date, proceeds=0, bank_account=None,
                          gain_loss_account=None, actor_user=None):  # Transactional asset disposal implementation.
    from .models import JournalEntry, JournalLine

    if asset.asset_status not in (AssetStatus.ACTIVE, AssetStatus.FULLY_DEPRECIATED):  # Only live/depreciated assets can be disposed.
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} is '{asset.asset_status}'; "
            f"only an active or fully-depreciated asset can be disposed.",
        )
    # Refuse to dispose while depreciation due up to the disposal date is still unposted:
    # the disposal journal would otherwise strip the cost/accum before those charges land,
    # understating the loss. Charges dated AFTER the disposal date are fine to orphan
    # (the asset's life is simply cut short).  # Preserve correct net book value at disposal.
    unposted_due = asset.schedule.filter(
        is_posted=False, depreciation_date__lte=disposal_date,  # Due on/before disposal date.
    ).count()
    if unposted_due:  # Disposal requires depreciation to be current through disposal date.
        raise DepreciationError(
            f"Asset {asset.document_number or asset.pk} has {unposted_due} unposted "
            f"depreciation charge(s) due on or before {disposal_date}; post depreciation "
            f"up to the disposal date before disposing.",
        )
    proceeds = int(proceeds or 0)  # Normalize proceeds to integer kobo.
    nbv = asset.cost - asset.accumulated_depreciation  # Net book value at disposal.
    gain_loss = proceeds - nbv  # >0 gain, <0 loss
    if proceeds > 0 and bank_account is None:  # Cash proceeds need a bank account.
        raise DepreciationError("A bank account is required to record disposal proceeds.")
    if gain_loss != 0 and gain_loss_account is None:  # Gain/loss must be posted to a P&L account.
        raise DepreciationError("A gain/loss account is required to record the disposal result.")

    ppe, accum, _ = _asset_accounts(asset)  # Resolve asset cost and accumulated depreciation accounts.
    period = resolve_period(asset.entity, disposal_date)  # Resolve disposal period.
    entry = JournalEntry.objects.create(
        entity=asset.entity, branch=asset.branch, date=disposal_date,  # Scope and date.
        period=period, source=JournalSource.BANK,  # Disposal source and period.
        narration=f"Disposal of {asset.name}", created_by=actor_user,  # Narration and actor.
    )
    line_no = 0  # Journal line counter.
    if asset.accumulated_depreciation > 0:  # Accumulated depreciation must be cleared.
        line_no += 1  # Advance line order.
        JournalLine.objects.create(entry=entry, account=accum, debit=asset.accumulated_depreciation,
                                   credit=0, description="Accumulated depreciation written back", line_no=line_no)  # Dr accum depreciation.
    if proceeds > 0:  # Cash proceeds are debited to bank.
        line_no += 1  # Advance line order.
        JournalLine.objects.create(entry=entry, account=bank_account.gl_account, debit=proceeds,
                                   credit=0, description="Disposal proceeds", line_no=line_no)  # Dr bank for proceeds.
    if gain_loss < 0:  # Proceeds below NBV create a loss.
        line_no += 1  # Advance line order.
        JournalLine.objects.create(entry=entry, account=gain_loss_account, debit=-gain_loss,
                                   credit=0, description="Loss on disposal", line_no=line_no)  # Dr loss account.
    elif gain_loss > 0:  # Proceeds above NBV create a gain.
        line_no += 1  # Advance line order.
        JournalLine.objects.create(entry=entry, account=gain_loss_account, debit=0,
                                   credit=gain_loss, description="Gain on disposal", line_no=line_no)  # Cr gain account.
    line_no += 1  # Final line removes asset cost.
    JournalLine.objects.create(entry=entry, account=ppe, debit=0, credit=asset.cost,
                               description="Asset cost removed", line_no=line_no)  # Cr PP&E cost.
    post_journal(entry, actor_user=actor_user)  # Validate and post disposal journal.

    asset.disposal_journal = entry  # Link asset to disposal journal.
    asset.disposal_date = disposal_date  # Persist disposal date.
    asset.asset_status = AssetStatus.DISPOSED  # Mark asset retired.
    asset.save(update_fields=["disposal_journal", "disposal_date", "asset_status", "updated_at"])

    record(  # Audit successful disposal.
        entity=asset.entity, action=FinanceAuditAction.ASSET_DISPOSED,  # Audit action.
        actor_user=actor_user, target=asset,  # Actor and target context.
        message=f"Disposed {asset.name}: proceeds {proceeds}, {'gain' if gain_loss >= 0 else 'loss'} {abs(gain_loss)} kobo.",  # Summary.
        journal_id=entry.pk, proceeds=proceeds, gain_loss=gain_loss, nbv=nbv,  # Structured disposal metadata.
    )
    return entry  # Return posted disposal journal.
