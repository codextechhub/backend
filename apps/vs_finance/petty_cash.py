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
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
)
from .exceptions import FinanceError, PettyCashError, PettyCashOverdrawError
from .posting import post_journal, resolve_period
from .receivables import compute_line_net, compute_tax


def gl_cash_on_hand(fund) -> int:
    """Live cash on hand for ``fund`` — the posted GL balance of its petty-cash account.

    The source of truth (an asset, signed to its natural debit balance). Used for the
    overdraw guard and to re-sync the fund's denormalised ``current_balance`` after each
    operation, so a stray direct journal to the account can never leave the guard or the
    stored mirror stale.
    """
    from .banking import gl_account_balance

    return gl_account_balance(fund.gl_account)


# --------------------------------------------------------------------------- #
# Establish / top up the float (Dr petty cash, Cr bank)                        #
# --------------------------------------------------------------------------- #

def establish_fund(fund, *, bank_account, amount, date, actor_user=None):
    """Move ``amount`` kobo of cash from ``bank_account`` into the fund's tin.

    The opening establishment of a float; also usable to permanently increase the imprest.
    Posts ``Dr petty cash, Cr bank`` and raises ``current_balance``. Records a durable
    rejection audit on any :class:`FinanceError`.
    """
    try:
        return _establish_fund_atomic(
            fund, bank_account=bank_account, amount=amount, date=date,
            actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_ESTABLISHED,
            exc=exc, actor_user=actor_user, target=fund,
        )
        raise


@transaction.atomic
def _establish_fund_atomic(fund, *, bank_account, amount, date, actor_user=None):
    from .models import JournalEntry, JournalLine

    amount = int(amount)
    if amount <= 0:
        raise PettyCashError("A petty cash establishment must be a positive amount.")
    if bank_account.entity_id != fund.entity_id:
        raise PettyCashError("The bank account belongs to a different entity.")

    period = resolve_period(fund.entity, date)
    entry = JournalEntry.objects.create(
        entity=fund.entity, branch=fund.branch, date=date, period=period,
        source=JournalSource.BANK, currency=fund.currency,
        narration=f"Establish petty cash float: {fund.name}",
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=fund.gl_account, debit=amount, credit=0,
        description=f"Petty cash float: {fund.name}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=amount,
        description=f"Cash to petty cash: {fund.name}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.save(update_fields=["current_balance", "updated_at"])

    record(
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_ESTABLISHED,
        actor_user=actor_user, target=fund,
        message=f"Established {amount} kobo into petty cash '{fund.name}'.",
        journal_id=entry.pk, amount=amount,
    )
    return entry


# --------------------------------------------------------------------------- #
# Voucher pricing + posting (Dr expense, Cr petty cash)                        #
# --------------------------------------------------------------------------- #

def price_voucher(voucher) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the voucher totals."""
    from .models import PettyCashVoucherLine

    for line in voucher.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            PettyCashVoucherLine.objects.filter(pk=line.pk).update(
                net_amount=net, tax_amount=tax,
            )
    voucher.recompute_totals(save=True)


def post_voucher(voucher, *, actor_user=None):
    """Price, validate and post a :class:`PettyCashVoucher`, relieving the fund's cash.

    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:
        return _post_voucher_atomic(voucher, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_REJECTED,
            exc=exc, actor_user=actor_user, target=voucher,
        )
        raise


@transaction.atomic
def _post_voucher_atomic(voucher, *, actor_user=None):
    from .models import JournalEntry, JournalLine, PettyCashFund

    if voucher.status != DocumentStatus.DRAFT:
        raise PettyCashError(
            f"Voucher {voucher.document_number or voucher.pk} is '{voucher.status}', "
            f"only a draft can be posted.",
        )

    price_voucher(voucher)
    if voucher.total <= 0:
        raise PettyCashError("A petty cash voucher must have a positive total to post.")

    # Lock the fund row so concurrent vouchers can't both pass the on-hand guard.
    fund = PettyCashFund.objects.select_for_update().get(pk=voucher.fund_id)
    if not fund.is_active:
        raise PettyCashError(f"Petty cash fund '{fund.name}' is inactive.")
    # Guard against the LIVE GL cash on hand (truth), not the denormalised mirror, so a
    # drifted mirror can never over- or under-authorise a payout.
    on_hand = gl_cash_on_hand(fund)
    if voucher.total > on_hand:
        raise PettyCashOverdrawError(
            fund_name=fund.name, requested=voucher.total, on_hand=on_hand,
        )

    period = resolve_period(voucher.entity, voucher.voucher_date)
    entry = JournalEntry.objects.create(
        entity=voucher.entity, branch=voucher.branch,
        date=voucher.voucher_date, period=period,
        source=JournalSource.BANK, currency=voucher.currency,
        narration=voucher.narration or f"Petty cash voucher {voucher.document_number or ''}".strip(),
        reference=voucher.reference, created_by=actor_user,
    )

    line_no = 0
    # Dr expense, grouped by (account, cost centre) so the cost-centre split survives into
    # the GL. Expense is P&L, so it carries the analytics; the input-tax line and the
    # petty-cash credit (below) do not.
    expense_by_key: dict[tuple[int, int | None], int] = defaultdict(int)
    expense_objs: dict[tuple[int, int | None], tuple] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}
    for line in voucher.lines.select_related(
        "expense_account", "tax_code__paid_account", "cost_center",
    ):
        key = (line.expense_account_id, line.cost_center_id)
        expense_by_key[key] += line.net_amount
        expense_objs[key] = (line.expense_account, line.cost_center)
        if line.tax_amount:
            tax_acc = line.tax_code.paid_account if line.tax_code_id else None
            if tax_acc is None:
                raise PettyCashError(
                    f"Tax code '{line.tax_code.code}' has no paid (input) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount
            tax_objs[tax_acc.id] = tax_acc

    for (acc_id, cc_id), amount in expense_by_key.items():
        if amount == 0:
            continue
        line_no += 1
        expense_account, cost_center = expense_objs[(acc_id, cc_id)]
        JournalLine.objects.create(
            entry=entry, account=expense_account, debit=amount, credit=0,
            description="Petty cash expense", cost_center=cost_center, line_no=line_no,
        )
    for acc_id, amount in tax_by_account.items():
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,
            description="Input tax", line_no=line_no,
        )
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=fund.gl_account, debit=0, credit=voucher.total,
        description=f"Petty cash: {fund.name}", line_no=line_no,
    )

    post_journal(entry, actor_user=actor_user)

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.save(update_fields=["current_balance", "updated_at"])

    voucher.journal = entry
    voucher.status = DocumentStatus.POSTED
    voucher.save(update_fields=["journal", "status", "updated_at"])

    record(
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_POSTED,
        actor_user=actor_user, target=voucher,
        message=f"Posted petty cash voucher ({voucher.total} kobo from '{fund.name}').",
        journal_id=entry.pk, total=voucher.total, tax=voucher.tax_total,
    )
    return voucher


@transaction.atomic
def void_voucher(voucher, *, actor_user=None):
    """Void a **posted** petty-cash voucher: reverse its journal and put the cash back.

    The "undo" for a voucher posted in error. Reverses the posting journal (a mirror
    entry that restores ``Dr petty cash, Cr expense``), re-syncs the fund's
    ``current_balance`` from the GL (so the cash returns to the tin), and marks the
    voucher CANCELLED. Only a POSTED voucher can be voided.
    """
    from .models import PettyCashFund

    if voucher.status != DocumentStatus.POSTED:
        raise PettyCashError(
            f"Only a posted voucher can be voided (this is '{voucher.status}').",
        )
    if voucher.journal_id is None:
        raise PettyCashError("Voucher has no posting journal to reverse.")

    # Lock the fund so the re-sync of current_balance is consistent under concurrency.
    fund = PettyCashFund.objects.select_for_update().get(pk=voucher.fund_id)

    from .posting import reverse_journal
    reverse_journal(voucher.journal, actor_user=actor_user)

    fund.current_balance = gl_cash_on_hand(fund)  # cash restored to the tin
    fund.save(update_fields=["current_balance", "updated_at"])

    voucher.status = DocumentStatus.CANCELLED
    voucher.save(update_fields=["status", "updated_at"])

    record(
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_VOIDED,
        actor_user=actor_user, target=voucher,
        message=f"Voided petty cash voucher {voucher.document_number or voucher.pk} "
                f"(reversed journal {voucher.journal_id}); {voucher.total} kobo back to "
                f"'{fund.name}'.",
        journal_id=voucher.journal_id, total=voucher.total,
    )
    return voucher


# --------------------------------------------------------------------------- #
# Replenishment (Dr petty cash, Cr bank — restore the float)                   #
# --------------------------------------------------------------------------- #

def replenish_fund(fund, *, bank_account, date, amount=None, actor_user=None):
    """Top the tin back up to its float (or by ``amount``): ``Dr petty cash, Cr bank``.

    With ``amount`` omitted, replenishes the exact shortfall so the fund is restored to
    ``float_amount``. Records a durable rejection audit on any :class:`FinanceError`.
    """
    try:
        return _replenish_fund_atomic(
            fund, bank_account=bank_account, date=date, amount=amount,
            actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_REPLENISHED,
            exc=exc, actor_user=actor_user, target=fund,
        )
        raise


@transaction.atomic
def _replenish_fund_atomic(fund, *, bank_account, date, amount=None, actor_user=None):
    from .models import JournalEntry, JournalLine, PettyCashFund

    fund = PettyCashFund.objects.select_for_update().get(pk=fund.pk)
    if bank_account.entity_id != fund.entity_id:
        raise PettyCashError("The bank account belongs to a different entity.")

    # Re-sync from the GL before sizing the top-up, so the shortfall is measured against
    # the real cash on hand (not a possibly-drifted mirror).
    fund.current_balance = gl_cash_on_hand(fund)
    top_up = fund.shortfall if amount is None else int(amount)
    if top_up <= 0:
        raise PettyCashError(
            "Nothing to replenish — the fund is already at (or above) its float."
            if amount is None else "Replenishment amount must be positive.",
        )

    period = resolve_period(fund.entity, date)
    entry = JournalEntry.objects.create(
        entity=fund.entity, branch=fund.branch, date=date, period=period,
        source=JournalSource.BANK, currency=fund.currency,
        narration=f"Replenish petty cash float: {fund.name}",
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=fund.gl_account, debit=top_up, credit=0,
        description=f"Replenish petty cash: {fund.name}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=top_up,
        description=f"Cash to petty cash: {fund.name}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.last_replenished_at = date
    fund.save(update_fields=["current_balance", "last_replenished_at", "updated_at"])

    record(
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_REPLENISHED,
        actor_user=actor_user, target=fund,
        message=f"Replenished {top_up} kobo into petty cash '{fund.name}'.",
        journal_id=entry.pk, amount=top_up,
    )
    return entry


# --------------------------------------------------------------------------- #
# Read-only status (low-balance / replenishment alerts)                        #
# --------------------------------------------------------------------------- #

def fund_status(entity, *, threshold_bps=2500) -> list:
    """Per-fund cash position with a low-balance flag for replenishment alerts.

    ``threshold_bps`` is the fraction of the float (in basis points; default 25%) at or
    below which a fund is flagged ``needs_replenish``. Returns one dict per active fund.
    """
    from .models import PettyCashFund

    rows = []
    qs = (
        PettyCashFund.objects
        .filter(entity=entity, is_active=True)
        .select_related("gl_account")
        .order_by("name")
    )
    for fund in qs:
        threshold = int(fund.float_amount) * threshold_bps // 10000
        on_hand = gl_cash_on_hand(fund)  # live GL truth, so alerts can't be misled by drift
        shortfall = max(int(fund.float_amount) - on_hand, 0)
        rows.append({
            "fund_id": fund.id, "name": fund.name,
            "gl_code": fund.gl_account.code,
            "float_amount": int(fund.float_amount),
            "current_balance": on_hand,
            "shortfall": shortfall,
            "needs_replenish": on_hand <= threshold,
            "last_replenished_at": fund.last_replenished_at,
        })
    return rows
