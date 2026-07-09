"""Petty-cash services — a small physical float run on the perpetual imprest system.

A :class:`~vs_finance.models.PettyCashFund` is a tin of cash a custodian holds for
day-to-day small spends. It runs **perpetually**: money moves through the GL the moment
it happens. The **GL petty-cash account is the source of truth** for cash on hand — the
overdraw guard reads it live, and the fund's ``current_balance`` is *re-synced from it*
after every operation (so it self-heals and can't silently drift).

Three moments touch the ledger:

* **Establish / top up** (:func:`establish_fund`) — move cash from the bank into the tin:
  ``Dr petty cash, Cr bank``. Raises ``current_balance``; first call sets the float.
* **Spend** (:func:`post_voucher`) — record a voucher slip's expenses as they are paid:
  ``Dr expense(s) (+ Dr input VAT), Cr petty cash``. Lowers ``current_balance``; a voucher
  exceeding the cash on hand is rejected (:class:`PettyCashOverdrawError`).
* **Replenish** (:func:`replenish_fund`) — restore the tin to its float after spending:
  ``Dr petty cash, Cr bank`` for the shortfall (or a given amount).

:func:`fund_status` is a read-only view used for low-balance / replenishment alerts.

All amounts are integer kobo; tax uses the same basis-point discipline as the rest of
the engine.
"""
from __future__ import annotations  # Defer annotation evaluation during app import.

from collections import defaultdict  # Groups voucher lines by account/cost-center.

from django.db import transaction  # Keeps petty-cash mutations atomic.

from .accounts import resolve_account  # Imported for account resolution consistency in finance services.
from .audit import record, record_rejection  # Finance audit helpers.
from .constants import (
    DocumentStatus,  # Finance document lifecycle statuses.
    FinanceAuditAction,  # Audit action enum values.
    JournalSource,  # Journal source enum values.
)
from .exceptions import FinanceError, PettyCashError, PettyCashOverdrawError  # Petty-cash and base finance errors.
from .posting import post_journal, resolve_period  # GL posting and period resolution helpers.
from .receivables import compute_line_net, compute_tax  # Shared line pricing/tax helpers.


def gl_cash_on_hand(fund) -> int:  # Read live GL balance for a petty-cash fund.
    """Live cash on hand for ``fund`` — the posted GL balance of its petty-cash account.

    The source of truth (an asset, signed to its natural debit balance). Used for the
    overdraw guard and to re-sync the fund's denormalised ``current_balance`` after each
    operation, so a stray direct journal to the account can never leave the guard or the
    stored mirror stale.
    """
    from .banking import gl_account_balance  # Local import avoids service import cycles.

    return gl_account_balance(fund.gl_account)  # Return signed asset balance of fund GL account.


# --------------------------------------------------------------------------- #
# Establish / top up the float (Dr petty cash, Cr bank)                        #
# --------------------------------------------------------------------------- #

def establish_fund(fund, *, bank_account, amount, date, actor_user=None):  # Public wrapper for float establishment/top-up.
    """Move ``amount`` kobo of cash from ``bank_account`` into the fund's tin.

    The opening establishment of a float; also usable to permanently increase the imprest.
    Posts ``Dr petty cash, Cr bank`` and raises ``current_balance``. Records a durable
    rejection audit on any :class:`FinanceError`.
    """
    try:  # Atomic worker performs the cash movement.
        return _establish_fund_atomic(  # Move cash into petty cash.
            fund, bank_account=bank_account, amount=amount, date=date,  # Funding bank, amount, and date.
            actor_user=actor_user,  # Acting user for posting/audit.
        )
    except FinanceError as exc:  # Failed establishment should be auditable.
        record_rejection(  # Record durable rejection.
            entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_ESTABLISHED,  # Audit action.
            exc=exc, actor_user=actor_user, target=fund,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _establish_fund_atomic(fund, *, bank_account, amount, date, actor_user=None):  # Transactional float establishment.
    from .models import JournalEntry, JournalLine  # Journal models used for cash transfer.

    amount = int(amount)  # Normalize amount to integer kobo.
    if amount <= 0:  # Establishment/top-up must move positive cash.
        raise PettyCashError("A petty cash establishment must be a positive amount.")
    if bank_account.entity_id != fund.entity_id:  # Bank and fund must belong to same entity.
        raise PettyCashError("The bank account belongs to a different entity.")

    period = resolve_period(fund.entity, date)  # Resolve transfer accounting period.
    entry = JournalEntry.objects.create(  # Create establishment journal header.
        entity=fund.entity, branch=fund.branch, date=date, period=period,  # Scope, date, and period.
        source=JournalSource.BANK, currency=fund.currency,  # Bank-source cash movement.
        narration=f"Establish petty cash float: {fund.name}",  # Journal narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(  # Debit petty cash asset.
        entry=entry, account=fund.gl_account, debit=amount, credit=0,  # Dr petty cash.
        description=f"Petty cash float: {fund.name}", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(  # Credit bank account.
        entry=entry, account=bank_account.gl_account, debit=0, credit=amount,  # Cr bank.
        description=f"Cash to petty cash: {fund.name}", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post transfer journal.

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.save(update_fields=["current_balance", "updated_at"])  # Persist denormalized balance mirror.

    record(  # Audit successful establishment/top-up.
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_ESTABLISHED,  # Audit action.
        actor_user=actor_user, target=fund,  # Actor and target context.
        message=f"Established {amount} kobo into petty cash '{fund.name}'.",  # Summary.
        journal_id=entry.pk, amount=amount,  # Structured metadata.
    )
    return entry  # Return posted transfer journal.


# --------------------------------------------------------------------------- #
# Voucher pricing + posting (Dr expense, Cr petty cash)                        #
# --------------------------------------------------------------------------- #

def price_voucher(voucher) -> None:  # Recalculate voucher line and header totals.
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the voucher totals."""
    from .models import PettyCashVoucherLine  # Local import avoids model import cycles.

    for line in voucher.lines.all():  # Reprice every voucher line.
        net = compute_line_net(line.quantity, line.unit_price)  # Compute net amount in kobo.
        rate = line.tax_code.rate_bps if line.tax_code_id else 0  # Use tax rate when a tax code exists.
        tax = compute_tax(net, rate)  # Compute input tax amount in kobo.
        if line.net_amount != net or line.tax_amount != tax:  # Avoid unnecessary writes.
            PettyCashVoucherLine.objects.filter(pk=line.pk).update(  # Persist recalculated amounts.
                net_amount=net, tax_amount=tax,  # Updated line totals.
            )
    voucher.recompute_totals(save=True)  # Roll line totals up to voucher header.


def post_voucher(voucher, *, actor_user=None):  # Public wrapper for petty-cash voucher posting.
    """Price, validate and post a :class:`PettyCashVoucher`, relieving the fund's cash.

    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:  # Atomic worker posts the voucher.
        return _post_voucher_atomic(voucher, actor_user=actor_user)  # Post voucher.
    except FinanceError as exc:  # Failed vouchers should be auditable.
        record_rejection(  # Record durable rejection.
            entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_REJECTED,  # Rejection action.
            exc=exc, actor_user=actor_user, target=voucher,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _post_voucher_atomic(voucher, *, actor_user=None):  # Transactional voucher posting implementation.
    from .models import JournalEntry, JournalLine, PettyCashFund  # Journal and fund models.

    if voucher.status != DocumentStatus.DRAFT:  # Only draft vouchers can post.
        raise PettyCashError(
            f"Voucher {voucher.document_number or voucher.pk} is '{voucher.status}', "
            f"only a draft can be posted.",
        )

    price_voucher(voucher)  # Ensure voucher line amounts and totals are current.
    if voucher.total <= 0:  # Voucher must spend a positive amount.
        raise PettyCashError("A petty cash voucher must have a positive total to post.")

    # Lock the fund row so concurrent vouchers can't both pass the on-hand guard.  # Prevent double-spend race.
    fund = PettyCashFund.objects.select_for_update().get(pk=voucher.fund_id)  # Lock fund row.
    if not fund.is_active:  # Inactive funds cannot pay vouchers.
        raise PettyCashError(f"Petty cash fund '{fund.name}' is inactive.")
    # Guard against the LIVE GL cash on hand (truth), not the denormalised mirror, so a
    # drifted mirror can never over- or under-authorise a payout.  # GL balance is authoritative.
    on_hand = gl_cash_on_hand(fund)  # Live petty-cash balance.
    if voucher.total > on_hand:  # Reject spends beyond cash on hand.
        raise PettyCashOverdrawError(
            fund_name=fund.name, requested=voucher.total, on_hand=on_hand,
        )

    period = resolve_period(voucher.entity, voucher.voucher_date)  # Resolve voucher period.
    entry = JournalEntry.objects.create(  # Create voucher journal header.
        entity=voucher.entity, branch=voucher.branch,  # Scope entity and optional branch.
        date=voucher.voucher_date, period=period,  # Voucher date and period.
        source=JournalSource.BANK, currency=voucher.currency,  # Bank/cash source and currency.
        narration=voucher.narration or f"Petty cash voucher {voucher.document_number or ''}".strip(),  # Narration.
        reference=voucher.reference, created_by=actor_user,  # External reference and actor.
    )

    line_no = 0  # Journal line counter.
    # Dr expense, grouped by (account, cost centre) so the cost-centre split survives into
    # the GL. Expense is P&L, so it carries the analytics; the input-tax line and the
    # petty-cash credit (below) do not.  # Preserve analytics where relevant.
    expense_by_key: dict[tuple[int, int | None], int] = defaultdict(int)  # Net expense grouped by account/cost center.
    expense_objs: dict[tuple[int, int | None], tuple] = {}  # Account/cost-center objects for grouped expense lines.
    tax_by_account: dict[int, int] = defaultdict(int)  # Input tax grouped by account.
    tax_objs: dict[int, object] = {}  # Tax account objects.
    for line in voucher.lines.select_related(  # Load posting targets for each voucher line.
        "expense_account", "tax_code__paid_account", "cost_center",  # Expense, tax, and analytics relations.
    ):
        key = (line.expense_account_id, line.cost_center_id)  # Group identity for expense line.
        expense_by_key[key] += line.net_amount  # Accumulate net expense amount.
        expense_objs[key] = (line.expense_account, line.cost_center)  # Store objects for journal creation.
        if line.tax_amount:  # Tax-bearing lines require an input tax account.
            tax_acc = line.tax_code.paid_account if line.tax_code_id else None  # Resolve input tax account.
            if tax_acc is None:  # Cannot post tax without an input account.
                raise PettyCashError(
                    f"Tax code '{line.tax_code.code}' has no paid (input) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount  # Accumulate tax amount.
            tax_objs[tax_acc.id] = tax_acc  # Store tax account.

    for (acc_id, cc_id), amount in expense_by_key.items():  # Emit grouped expense debits.
        if amount == 0:  # Skip empty groups.
            continue
        line_no += 1  # Advance line number.
        expense_account, cost_center = expense_objs[(acc_id, cc_id)]  # Retrieve objects for this group.
        JournalLine.objects.create(  # Debit expense account.
            entry=entry, account=expense_account, debit=amount, credit=0,  # Dr expense.
            description="Petty cash expense", cost_center=cost_center, line_no=line_no,  # Label and analytics.
        )
    for acc_id, amount in tax_by_account.items():  # Emit grouped input-tax debits.
        line_no += 1  # Advance line number.
        JournalLine.objects.create(  # Debit input tax account.
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,  # Dr input tax.
            description="Input tax", line_no=line_no,  # Label and order.
        )
    line_no += 1  # Final line credits petty cash.
    JournalLine.objects.create(  # Credit petty cash for voucher total.
        entry=entry, account=fund.gl_account, debit=0, credit=voucher.total,  # Cr petty cash.
        description=f"Petty cash: {fund.name}", line_no=line_no,  # Label and order.
    )

    post_journal(entry, actor_user=actor_user)  # Validate and post voucher journal.

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.save(update_fields=["current_balance", "updated_at"])  # Persist balance mirror.

    voucher.journal = entry  # Link voucher to posting journal.
    voucher.status = DocumentStatus.POSTED  # Mark voucher posted.
    voucher.save(update_fields=["journal", "status", "updated_at"])  # Persist posting fields.

    record(  # Audit successful voucher post.
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_POSTED,  # Audit action.
        actor_user=actor_user, target=voucher,  # Actor and target context.
        message=f"Posted petty cash voucher ({voucher.total} kobo from '{fund.name}').",  # Summary.
        journal_id=entry.pk, total=voucher.total, tax=voucher.tax_total,  # Structured metadata.
    )
    return voucher  # Return posted voucher.


@transaction.atomic
def void_voucher(voucher, *, actor_user=None):  # Reverse a posted petty-cash voucher.
    """Void a **posted** petty-cash voucher: reverse its journal and put the cash back.

    The "undo" for a voucher posted in error. Reverses the posting journal (a mirror
    entry that restores ``Dr petty cash, Cr expense``), re-syncs the fund's
    ``current_balance`` from the GL (so the cash returns to the tin), and marks the
    voucher CANCELLED. Only a POSTED voucher can be voided.
    """
    from .models import PettyCashFund  # Local import avoids model import cycles.

    if voucher.status != DocumentStatus.POSTED:  # Only posted vouchers have journals to reverse.
        raise PettyCashError(
            f"Only a posted voucher can be voided (this is '{voucher.status}').",
        )
    if voucher.journal_id is None:  # Posted voucher should always have a posting journal.
        raise PettyCashError("Voucher has no posting journal to reverse.")

    # Lock the fund so the re-sync of current_balance is consistent under concurrency.  # Avoid stale mirror writes.
    fund = PettyCashFund.objects.select_for_update().get(pk=voucher.fund_id)  # Lock fund row.

    from .posting import reverse_journal  # Local import avoids circular service dependency.
    reverse_journal(voucher.journal, actor_user=actor_user)  # Post mirror reversal journal.

    fund.current_balance = gl_cash_on_hand(fund)  # cash restored to the tin
    fund.save(update_fields=["current_balance", "updated_at"])  # Persist balance mirror.

    voucher.status = DocumentStatus.CANCELLED  # Mark voucher voided/cancelled.
    voucher.save(update_fields=["status", "updated_at"])  # Persist lifecycle change.

    record(  # Audit successful void.
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_VOIDED,  # Audit action.
        actor_user=actor_user, target=voucher,  # Actor and target context.
        message=f"Voided petty cash voucher {voucher.document_number or voucher.pk} "  # Human-readable summary.
                f"(reversed journal {voucher.journal_id}); {voucher.total} kobo back to "  # Reversal and amount.
                f"'{fund.name}'.",  # Fund name.
        journal_id=voucher.journal_id, total=voucher.total,  # Structured metadata.
    )
    return voucher  # Return voided voucher.


# --------------------------------------------------------------------------- #
# Replenishment (Dr petty cash, Cr bank — restore the float)                   #
# --------------------------------------------------------------------------- #

def replenish_fund(fund, *, bank_account, date, amount=None, actor_user=None):  # Public wrapper for petty-cash replenishment.
    """Top the tin back up to its float (or by ``amount``): ``Dr petty cash, Cr bank``.

    With ``amount`` omitted, replenishes the exact shortfall so the fund is restored to
    ``float_amount``. Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:  # Atomic worker posts the replenishment.
        return _replenish_fund_atomic(  # Move cash from bank to petty cash.
            fund, bank_account=bank_account, date=date, amount=amount,  # Bank, date, and optional amount.
            actor_user=actor_user,  # Acting user.
        )
    except FinanceError as exc:  # Failed replenishments should be auditable.
        record_rejection(  # Record durable rejection.
            entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_REPLENISHED,  # Audit action.
            exc=exc, actor_user=actor_user, target=fund,  # Error, actor, and target context.
        )
        raise  # Preserve original finance exception.


@transaction.atomic
def _replenish_fund_atomic(fund, *, bank_account, date, amount=None, actor_user=None):  # Transactional replenishment.
    from .models import JournalEntry, JournalLine, PettyCashFund  # Journal and fund models.

    fund = PettyCashFund.objects.select_for_update().get(pk=fund.pk)  # Lock fund row while sizing top-up.
    if bank_account.entity_id != fund.entity_id:  # Bank and fund must be in same entity.
        raise PettyCashError("The bank account belongs to a different entity.")

    # Re-sync from the GL before sizing the top-up, so the shortfall is measured against
    # the real cash on hand (not a possibly-drifted mirror).  # GL remains source of truth.
    fund.current_balance = gl_cash_on_hand(fund)  # Refresh denormalized balance before calculating shortfall.
    top_up = fund.shortfall if amount is None else int(amount)  # Use automatic shortfall or explicit amount.
    if top_up <= 0:  # Nothing valid to replenish.
        raise PettyCashError(
            "Nothing to replenish — the fund is already at (or above) its float."
            if amount is None else "Replenishment amount must be positive.",
        )

    period = resolve_period(fund.entity, date)  # Resolve replenishment period.
    entry = JournalEntry.objects.create(  # Create replenishment journal header.
        entity=fund.entity, branch=fund.branch, date=date, period=period,  # Scope/date/period.
        source=JournalSource.BANK, currency=fund.currency,  # Bank-source cash movement.
        narration=f"Replenish petty cash float: {fund.name}",  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(  # Debit petty cash account.
        entry=entry, account=fund.gl_account, debit=top_up, credit=0,  # Dr petty cash.
        description=f"Replenish petty cash: {fund.name}", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(  # Credit bank account.
        entry=entry, account=bank_account.gl_account, debit=0, credit=top_up,  # Cr bank.
        description=f"Cash to petty cash: {fund.name}", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post replenishment journal.

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.last_replenished_at = date  # Record replenishment date.
    fund.save(update_fields=["current_balance", "last_replenished_at", "updated_at"])  # Persist fund state.

    record(  # Audit successful replenishment.
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_REPLENISHED,  # Audit action.
        actor_user=actor_user, target=fund,  # Actor and target context.
        message=f"Replenished {top_up} kobo into petty cash '{fund.name}'.",  # Summary.
        journal_id=entry.pk, amount=top_up,  # Structured metadata.
    )
    return entry  # Return posted replenishment journal.


# --------------------------------------------------------------------------- #
# Read-only status (low-balance / replenishment alerts)                        #
# --------------------------------------------------------------------------- #

def fund_status(entity, *, threshold_bps=2500) -> list:  # Return active petty-cash fund status rows.
    """Per-fund cash position with a low-balance flag for replenishment alerts.

    ``threshold_bps`` is the fraction of the float (in basis points; default 25%) at or
    below which a fund is flagged ``needs_replenish``. Returns one dict per active fund.
    """
    from .models import PettyCashFund  # Local import avoids model import cycles.

    rows = []  # Response rows.
    qs = (  # Active funds for this entity.
        PettyCashFund.objects  # Start from fund manager.
        .filter(entity=entity, is_active=True)  # Only active funds in entity.
        .select_related("gl_account")  # Load GL account for balance and code.
        .order_by("name")  # Stable display order.
    )
    for fund in qs:  # Build status for each fund.
        threshold = int(fund.float_amount) * threshold_bps // 10000  # Low-balance threshold in kobo.
        on_hand = gl_cash_on_hand(fund)  # live GL truth, so alerts can't be misled by drift
        shortfall = max(int(fund.float_amount) - on_hand, 0)  # Amount needed to restore float.
        rows.append({  # Append template/API row.
            "fund_id": fund.id, "name": fund.name,  # Fund identity.
            "gl_code": fund.gl_account.code,  # Petty-cash GL account code.
            "float_amount": int(fund.float_amount),  # Target imprest float.
            "current_balance": on_hand,  # Live cash on hand.
            "shortfall": shortfall,  # Replenishment shortfall.
            "needs_replenish": on_hand <= threshold,  # Low-balance flag.
            "last_replenished_at": fund.last_replenished_at,  # Last replenishment date.
        })
    return rows  # Return active fund statuses.
