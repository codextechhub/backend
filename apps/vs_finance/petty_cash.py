"""Petty-cash services - a small physical float run on the perpetual imprest system.

A :class:`~vs_finance.models.PettyCashFund` is a tin of cash a custodian holds for
day-to-day small spends. It runs **perpetually**: money moves through the GL the moment
it happens. The **GL petty-cash account is the source of truth** for cash on hand - the
overdraw guard reads it live, and the fund's ``current_balance`` is *re-synced from it*
after every operation (so it self-heals and can't silently drift).

Three moments touch the ledger:

* **Establish / top up** (:func:`establish_fund`) - move cash from the bank into the tin:
  ``Dr petty cash, Cr bank``. Raises ``current_balance``; first call sets the float.
* **Spend** (:func:`post_voucher`) - record a voucher slip's expenses as they are paid:
  ``Dr expense(s) (+ Dr input VAT), Cr petty cash``. Lowers ``current_balance``; a voucher
  exceeding the cash on hand is rejected (:class:`PettyCashOverdrawError`).
* **Replenish** (:func:`replenish_fund`) - restore the tin to its float after spending:
  ``Dr petty cash, Cr bank`` for the shortfall (or a given amount).
* **Return to the bank** (:func:`post_petty_cash_return`) - the custodian counts the
  tin and cash goes back to the bank, to cut the float or to close the fund:
  ``Dr bank, Cr petty cash``, with any count difference against the books posted to
  the cash over and short account. :func:`void_petty_cash_return` undoes one while
  its bank side is unmatched; :func:`reopen_fund` brings a closed fund back.

Every movement between the tin and a bank uses a bank account of the fund's own
branch (:func:`_require_fund_branch_bank`), checked here as well as at the request
boundary, so no caller can carry Ikeja's cash into Lekki's bank. A closed fund takes
no voucher, top-up, void or float change until it is reopened.

:func:`fund_status` is a read-only view used for low-balance / replenishment alerts.

All amounts are integer kobo; tax uses the same basis-point discipline as the rest of
the engine.
"""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from vs_config.display import format_date

from .audit import record, record_rejection
from .constants import (
    AccountMappingKey,
    DocumentStatus,
    FinanceAuditAction,
    JournalSource,
    PettyCashReturnKind,
)
from .exceptions import FinanceError, PettyCashError, PettyCashOverdrawError
from .money import format_naira
from .posting import post_journal, resolve_period
from .receivables import compute_line_net, compute_tax
from .wording import state_word


# Read live GL balance for a petty-cash fund.
def gl_cash_on_hand(fund) -> int:
    """Live cash on hand for ``fund`` - the posted GL balance of its petty-cash account.

    The source of truth (an asset, signed to its natural debit balance). Used for the
    overdraw guard and to re-sync the fund's denormalised ``current_balance`` after each
    operation, so a stray direct journal to the account can never leave the guard or the
    stored mirror stale.
    """
    from .banking import gl_account_balance

    return gl_account_balance(fund.gl_account)  # Return signed asset balance of fund GL account.


def _branch_name(row) -> str:
    """The name of ``row``'s branch, for a refusal message."""
    return row.branch.name if row.branch_id else "no branch yet"


def _require_fund_branch_bank(fund, bank_account) -> None:
    """Refuse a cash movement between ``fund`` and a bank account of another branch.

    A petty cash float is one branch's cash, and so is a bank account: Ikeja's tin
    is topped up from, and banked into, Ikeja's own account. The two are compared
    by :func:`vs_rbac.scoping.same_transaction_branch`, so at a tenant with one
    branch a fund or an account not yet given a branch is that branch's.
    """
    from vs_rbac.scoping import same_transaction_branch

    if bank_account.entity_id != fund.entity_id:
        raise PettyCashError("The bank account belongs to a different entity.")
    if same_transaction_branch(fund.entity.tenant_id, fund.branch_id, bank_account.branch_id):
        return
    raise PettyCashError(
        f"Petty cash fund '{fund.name}' belongs to {_branch_name(fund)} and "
        f"{bank_account.name} belongs to {_branch_name(bank_account)}. Move the fund's "
        f"cash only through a {_branch_name(fund)} bank account.",
    )


def _refuse_closed(fund) -> None:
    """Refuse any movement on a closed fund, naming the way back."""
    if fund.is_closed:
        raise PettyCashError(
            f"Petty cash fund '{fund.name}' was closed on "
            f"{format_date(fund.closed_on, fund.entity.tenant)}. "
            f"Reopen it first.",
        )


# --------------------------------------------------------------------------- #
# Establish / top up the float (Dr petty cash, Cr bank)                        #
# --------------------------------------------------------------------------- #

# Public wrapper for float establishment/top-up.
def establish_fund(fund, *, bank_account, amount, date, actor_user=None):
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
        raise


@transaction.atomic
# Transactional float establishment.
def _establish_fund_atomic(fund, *, bank_account, amount, date, actor_user=None):
    from .models import JournalEntry, JournalLine

    amount = int(amount)  # Normalize amount to integer kobo.
    if amount <= 0:  # Establishment/top-up must move positive cash.
        raise PettyCashError("A petty cash establishment must be a positive amount.")
    _refuse_closed(fund)
    _require_fund_branch_bank(fund, bank_account)

    period = resolve_period(fund.entity, date)  # Resolve transfer accounting period.
    entry = JournalEntry.objects.create(
        entity=fund.entity, branch=fund.branch, date=date, period=period,  # Scope, date, and period.
        source=JournalSource.BANK, currency=fund.currency,  # Bank-source cash movement.
        narration=f"Establish petty cash float: {fund.name}",  # Journal narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(
        entry=entry, account=fund.gl_account, debit=amount, credit=0,  # Dr petty cash.
        description=f"Petty cash float: {fund.name}", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=amount,  # Cr bank.
        description=f"Cash to petty cash: {fund.name}", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post transfer journal.

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.save(update_fields=["current_balance", "updated_at"])

    record(  # Audit successful establishment/top-up.
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_ESTABLISHED,  # Audit action.
        actor_user=actor_user, target=fund,  # Actor and target context.
        message=f"Established {format_naira(amount)} into petty cash '{fund.name}'.",  # Summary.
        journal_id=entry.pk, amount=amount,  # Structured metadata.
    )
    return entry  # Return posted transfer journal.


# --------------------------------------------------------------------------- #
# Voucher pricing + posting (Dr expense, Cr petty cash)                        #
# --------------------------------------------------------------------------- #

# Recalculate voucher line and header totals.
def price_voucher(voucher) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the voucher totals."""
    from .models import PettyCashVoucherLine

    for line in voucher.lines.all():  # Reprice every voucher line.
        net = compute_line_net(line.quantity, line.unit_price)  # Compute net amount in kobo.
        rate = line.tax_code.rate_bps if line.tax_code_id else 0  # Use tax rate when a tax code exists.
        tax = compute_tax(net, rate)  # Compute input tax amount in kobo.
        if line.net_amount != net or line.tax_amount != tax:  # Avoid unnecessary writes.
            PettyCashVoucherLine.objects.filter(pk=line.pk).update(
                net_amount=net, tax_amount=tax,  # Updated line totals.
            )
    voucher.recompute_totals(save=True)  # Roll line totals up to voucher header.


# Public wrapper for petty-cash voucher posting.
def post_voucher(voucher, *, actor_user=None):
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
        raise


@transaction.atomic
# Transactional voucher posting implementation.
def _post_voucher_atomic(voucher, *, actor_user=None):
    from .models import JournalEntry, JournalLine, PettyCashFund

    if voucher.status != DocumentStatus.DRAFT:  # Only draft vouchers can post.
        raise PettyCashError(
            f"Voucher {voucher.document_number or voucher.pk} is {state_word(voucher)}, "
            f"only a draft can be posted.",
        )

    price_voucher(voucher)  # Ensure voucher line amounts and totals are current.
    if voucher.total <= 0:  # Voucher must spend a positive amount.
        raise PettyCashError("A petty cash voucher must have a positive total to post.")

    # Lock the fund row so concurrent vouchers can't both pass the on-hand guard.  # Prevent double-spend race.
    fund = PettyCashFund.objects.select_for_update().get(pk=voucher.fund_id)
    _refuse_closed(fund)
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
    entry = JournalEntry.objects.create(
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
    for line in voucher.lines.select_related(
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
        JournalLine.objects.create(
            entry=entry, account=expense_account, debit=amount, credit=0,  # Dr expense.
            description="Petty cash expense", cost_center=cost_center, line_no=line_no,  # Label and analytics.
        )
    for acc_id, amount in tax_by_account.items():  # Emit grouped input-tax debits.
        line_no += 1  # Advance line number.
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,  # Dr input tax.
            description="Input tax", line_no=line_no,  # Label and order.
        )
    line_no += 1  # Final line credits petty cash.
    JournalLine.objects.create(
        entry=entry, account=fund.gl_account, debit=0, credit=voucher.total,  # Cr petty cash.
        description=f"Petty cash: {fund.name}", line_no=line_no,  # Label and order.
    )

    post_journal(entry, actor_user=actor_user)  # Validate and post voucher journal.

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.save(update_fields=["current_balance", "updated_at"])

    voucher.journal = entry  # Link voucher to posting journal.
    voucher.status = DocumentStatus.POSTED  # Mark voucher posted.
    voucher.save(update_fields=["journal", "status", "updated_at"])

    record(  # Audit successful voucher post.
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_POSTED,  # Audit action.
        actor_user=actor_user, target=voucher,  # Actor and target context.
        message=f"Posted petty cash voucher ({format_naira(voucher.total)} from '{fund.name}').",  # Summary.
        journal_id=entry.pk, total=voucher.total, tax=voucher.tax_total,  # Structured metadata.
    )
    return voucher  # Return posted voucher.


@transaction.atomic
# Reverse a posted petty-cash voucher.
def void_voucher(voucher, *, actor_user=None):
    """Void a **posted** petty-cash voucher: reverse its journal and put the cash back.

    The "undo" for a voucher posted in error. Reverses the posting journal (a mirror
    entry that restores ``Dr petty cash, Cr expense``), re-syncs the fund's
    ``current_balance`` from the GL (so the cash returns to the tin), and marks the
    voucher CANCELLED. Only a POSTED voucher can be voided, and not while its fund
    is closed: the cash would come back into a tin whose cash has already been
    banked, so the fund is reopened first.
    """
    from .models import PettyCashFund

    if voucher.status != DocumentStatus.POSTED:  # Only posted vouchers have journals to reverse.
        raise PettyCashError(
            f"Only a posted voucher can be voided (this is {state_word(voucher)}).",
        )
    if voucher.journal_id is None:  # Posted voucher should always have a posting journal.
        raise PettyCashError("Voucher has no posting journal to reverse.")

    # Lock the fund so the re-sync of current_balance is consistent under concurrency.  # Avoid stale mirror writes.
    fund = PettyCashFund.objects.select_for_update().get(pk=voucher.fund_id)
    _refuse_closed(fund)

    from .posting import reverse_journal
    reverse_journal(voucher.journal, actor_user=actor_user, document_owner=voucher)  # Post mirror reversal journal.

    fund.current_balance = gl_cash_on_hand(fund)  # cash restored to the tin
    fund.save(update_fields=["current_balance", "updated_at"])

    voucher.status = DocumentStatus.CANCELLED  # Mark voucher voided/cancelled.
    voucher.save(update_fields=["status", "updated_at"])

    record(  # Audit successful void.
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_VOIDED,  # Audit action.
        actor_user=actor_user, target=voucher,  # Actor and target context.
        message=f"Voided petty cash voucher {voucher.document_number or voucher.pk} "  # Human-readable summary.
                f"(reversed journal {voucher.journal_id}); {format_naira(voucher.total)} back to "  # Reversal and amount.
                f"'{fund.name}'.",  # Fund name.
        journal_id=voucher.journal_id, total=voucher.total,  # Structured metadata.
    )
    return voucher  # Return voided voucher.


# --------------------------------------------------------------------------- #
# Replenishment (Dr petty cash, Cr bank - restore the float)                   #
# --------------------------------------------------------------------------- #

# Public wrapper for petty-cash replenishment.
def replenish_fund(fund, *, bank_account, date, amount=None, actor_user=None):
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
        raise


@transaction.atomic
# Transactional replenishment.
def _replenish_fund_atomic(fund, *, bank_account, date, amount=None, actor_user=None):
    from .models import JournalEntry, JournalLine, PettyCashFund

    fund = PettyCashFund.objects.select_for_update().get(pk=fund.pk)
    _refuse_closed(fund)
    _require_fund_branch_bank(fund, bank_account)

    # Re-sync from the GL before sizing the top-up, so the shortfall is measured against
    # the real cash on hand (not a possibly-drifted mirror).  # GL remains source of truth.
    fund.current_balance = gl_cash_on_hand(fund)  # Refresh denormalized balance before calculating shortfall.
    top_up = fund.shortfall if amount is None else int(amount)  # Use automatic shortfall or explicit amount.
    if top_up <= 0:  # Nothing valid to replenish.
        raise PettyCashError(
            "Nothing to replenish - the fund is already at (or above) its float."
            if amount is None else "Replenishment amount must be positive.",
        )

    period = resolve_period(fund.entity, date)  # Resolve replenishment period.
    entry = JournalEntry.objects.create(
        entity=fund.entity, branch=fund.branch, date=date, period=period,  # Scope/date/period.
        source=JournalSource.BANK, currency=fund.currency,  # Bank-source cash movement.
        narration=f"Replenish petty cash float: {fund.name}",  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(
        entry=entry, account=fund.gl_account, debit=top_up, credit=0,  # Dr petty cash.
        description=f"Replenish petty cash: {fund.name}", line_no=1,  # Line label and order.
    )
    JournalLine.objects.create(
        entry=entry, account=bank_account.gl_account, debit=0, credit=top_up,  # Cr bank.
        description=f"Cash to petty cash: {fund.name}", line_no=2,  # Line label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post replenishment journal.

    fund.current_balance = gl_cash_on_hand(fund)  # re-sync from the GL (truth)
    fund.last_replenished_at = date  # Record replenishment date.
    fund.save(update_fields=["current_balance", "last_replenished_at", "updated_at"])

    record(  # Audit successful replenishment.
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_REPLENISHED,  # Audit action.
        actor_user=actor_user, target=fund,  # Actor and target context.
        message=f"Replenished {format_naira(top_up)} into petty cash '{fund.name}'.",  # Summary.
        journal_id=entry.pk, amount=top_up,  # Structured metadata.
    )
    return entry  # Return posted replenishment journal.


# --------------------------------------------------------------------------- #
# Draft vouchers, fund details                                                 #
# --------------------------------------------------------------------------- #

@transaction.atomic
def cancel_voucher(voucher, *, actor_user=None):
    """Cancel a **draft** voucher that will never be paid.

    A draft has touched no ledger, so cancelling it writes no journal; it only takes
    the voucher out of the way. A draft left open blocks the fund's closure, so a
    custodian who wrote one in error needs this to close the fund. A posted voucher
    is voided instead (:func:`void_voucher`).
    """
    from .models import PettyCashVoucher

    voucher = PettyCashVoucher.objects.select_for_update().get(pk=voucher.pk)
    if voucher.status != DocumentStatus.DRAFT:
        raise PettyCashError(
            f"Only a draft voucher can be cancelled; {voucher.document_number or voucher.pk} "
            f"is {state_word(voucher)}. A posted voucher is voided instead.",
        )
    voucher.status = DocumentStatus.CANCELLED
    voucher.save(update_fields=["status", "updated_at"])
    record(
        entity=voucher.entity, action=FinanceAuditAction.PETTY_CASH_VOUCHER_CANCELLED,
        actor_user=actor_user, target=voucher,
        message=f"Cancelled draft petty cash voucher {voucher.document_number or voucher.pk}.",
        total=voucher.total,
    )
    return voucher


#: Fund fields a plain edit may change, in the order the audit entry lists them.
_EDITABLE_FUND_FIELDS = ("name", "custodian_id", "custodian_name", "float_amount", "is_active")


@transaction.atomic
def change_fund_details(fund, *, actor_user=None, **changes):
    """Apply a plain edit to ``fund`` and record what it changed.

    An edit never moves cash, so it may not do what only a cash movement can:

    * lowering the float below the cash the fund holds would leave notes in the tin
      above its float with no record of where they go; that is a float reduction,
      which banks the excess (:func:`post_petty_cash_return`);
    * deactivating a fund that still holds cash would strand it on the balance
      sheet; that is a closure, which banks it;
    * a closed fund's float and activity change only by reopening it
      (:func:`reopen_fund`).

    The custodian and the float are who answers for the cash and how much of it
    there is, so every change is audited with its before and after, under the
    fund's branch. Returns the saved fund.
    """
    from .models import PettyCashFund

    fund = PettyCashFund.objects.select_for_update().get(pk=fund.pk)
    unknown = set(changes) - set(_EDITABLE_FUND_FIELDS)
    if unknown:
        raise ValueError(f"Not editable on a petty cash fund: {', '.join(sorted(unknown))}.")
    before = {f: getattr(fund, f) for f in _EDITABLE_FUND_FIELDS}
    on_hand = gl_cash_on_hand(fund)

    if fund.is_closed and (
            ("float_amount" in changes and int(changes["float_amount"]) != fund.float_amount)
            or ("is_active" in changes and bool(changes["is_active"]) != fund.is_active)):
        _refuse_closed(fund)
    if "float_amount" in changes:
        new_float = int(changes["float_amount"])
        if new_float < fund.float_amount and new_float < on_hand:
            raise PettyCashError(
                f"Petty cash fund '{fund.name}' holds {format_naira(on_hand)}, more than a "
                f"float of {format_naira(new_float)}. Reduce the float with a return, which "
                f"banks the cash above it.",
            )
    if changes.get("is_active") is False and fund.is_active and on_hand != 0:
        raise PettyCashError(
            f"Petty cash fund '{fund.name}' still holds {format_naira(on_hand)} on its books. "
            f"Close the fund instead, which banks the cash and records the count.",
        )

    for field, value in changes.items():
        setattr(fund, field, value)
    fund.save()

    after = {f: getattr(fund, f) for f in _EDITABLE_FUND_FIELDS}
    changed = [f for f in _EDITABLE_FUND_FIELDS if before[f] != after[f]]
    if changed:
        record(
            entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_FUND_UPDATED,
            actor_user=actor_user, target=fund,
            message=f"Changed petty cash fund '{fund.name}': {', '.join(changed)}.",
            before={f: before[f] for f in changed}, after={f: after[f] for f in changed},
        )
    return fund


# --------------------------------------------------------------------------- #
# Returning cash to the bank: reduce the float, close the fund                 #
# --------------------------------------------------------------------------- #

#: A return in these statuses is still on its way to the books. A fund has at most
#: one, so two counts of the same tin are never banked side by side.
_OPEN_RETURN_STATUSES = (DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED)

#: A voucher in these statuses has not yet been booked, so the fund cannot close
#: under it: its cash is either still in the tin or was spent without a record.
_OPEN_VOUCHER_STATUSES = (
    DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED,
)


def validate_petty_cash_return(ret) -> None:
    """Refuse a return that could never post, before it is saved, routed or posted.

    Write-free, so the request, the approval route's preflight and the posting all
    run the same rules and cannot disagree:

    * the fund is open (not closed), and a reduction needs it active as well;
    * the return carries the fund's branch, and the bank account is an active one of
      that same branch (:func:`_require_fund_branch_bank`). A closure that banks
      nothing names no bank account;
    * a **reduction** lowers the float to something above zero (a float cut to
      nothing is a closure), banks a positive amount no larger than the count, and
      leaves the tin holding no more than the new float;
    * a **closure** banks everything counted, and no voucher of the fund is still a
      draft or waiting on approval;
    * a count that differs from the books says why, and the cash over and short
      account resolves;
    * no other return of the fund is waiting on approval.

    That the fund's books and float have not moved since the count is checked only
    at posting (:func:`post_petty_cash_return`), where the fund row is locked.
    """
    from vs_rbac.scoping import same_transaction_branch

    from .account_mappings import resolve_mapped_account
    from .models import PettyCashReturn

    fund = ret.fund
    if fund.entity_id != ret.entity_id:
        raise PettyCashError("The petty cash fund belongs to other books.")
    _refuse_closed(fund)
    if not same_transaction_branch(fund.entity.tenant_id, ret.branch_id, fund.branch_id):
        raise PettyCashError(
            f"This return belongs to {_branch_name(ret)} and petty cash fund '{fund.name}' "
            f"to {_branch_name(fund)}.",
        )

    counted, amount = int(ret.counted_amount), int(ret.amount)
    if counted < 0 or amount < 0:
        raise PettyCashError("A count and the cash banked cannot be negative.")

    if ret.kind == PettyCashReturnKind.REDUCE:
        new_float = int(ret.new_float_amount)
        if not fund.is_active:
            raise PettyCashError(
                f"Petty cash fund '{fund.name}' is inactive. Close it to bank its cash.",
            )
        if new_float <= 0:
            raise PettyCashError(
                "A float cut to nothing is a closure. Close the fund instead.",
            )
        if new_float >= int(ret.previous_float_amount):
            raise PettyCashError(
                f"The new float must be lower than the current float of "
                f"{format_naira(ret.previous_float_amount)}.",
            )
        if amount <= 0:
            raise PettyCashError(
                f"Nothing to bank: the tin holds {format_naira(counted)}, no more than the "
                f"new float of {format_naira(new_float)}. Change the float on the fund instead.",
            )
        if amount > counted:
            raise PettyCashError(
                f"Cannot bank {format_naira(amount)} when the count found "
                f"{format_naira(counted)}.",
            )
        if counted - amount > new_float:
            raise PettyCashError(
                f"The tin would keep {format_naira(counted - amount)} against a float of "
                f"{format_naira(new_float)}. Bank at least {format_naira(counted - new_float)}.",
            )
    elif ret.kind == PettyCashReturnKind.CLOSE:
        if int(ret.new_float_amount) != 0 or amount != counted:
            raise PettyCashError("A closure banks everything counted and leaves no float.")
        open_vouchers = list(
            fund.vouchers.filter(status__in=_OPEN_VOUCHER_STATUSES)
            .order_by("id").values_list("document_number", flat=True)[:6]
        )
        if open_vouchers:
            raise PettyCashError(
                f"Petty cash fund '{fund.name}' still has vouchers not yet posted "
                f"({', '.join(n or '-' for n in open_vouchers)}). Post or cancel them first.",
            )
    else:
        raise PettyCashError(f"Unknown petty cash return kind '{ret.kind}'.")

    bank = ret.bank_account
    if amount > 0 and bank is None:
        raise PettyCashError("Name the bank account the cash is banked into.")
    if bank is not None:
        if not bank.is_active:
            raise PettyCashError(f"Bank account {bank.name} is closed.")
        _require_fund_branch_bank(fund, bank)

    if ret.difference:
        if not (ret.difference_reason or "").strip():
            raise PettyCashError(
                f"The count found {format_naira(counted)} against "
                f"{format_naira(ret.book_balance)} on the books. Say why they differ.",
            )
        resolve_mapped_account(ret.entity, AccountMappingKey.CASH_OVER_SHORT)

    waiting = (
        PettyCashReturn.objects.filter(fund_id=fund.pk, status__in=_OPEN_RETURN_STATUSES)
        .exclude(pk=ret.pk).values_list("document_number", flat=True).first()
    )
    if waiting is not None:
        raise PettyCashError(
            f"Return {waiting} of petty cash fund '{fund.name}' is still waiting on approval. "
            f"Settle it before counting the tin again.",
        )


def post_petty_cash_return(ret, *, actor_user=None):
    """Post a draft or approved return, recording a durable rejection on failure."""
    try:
        return _post_petty_cash_return_atomic(ret, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_RETURN_POSTED,
            exc=exc, actor_user=actor_user, target=ret,
        )
        raise


def _return_lines(ret, fund, over_short):
    """The journal lines of ``ret``: (account, debit, credit, description), in order.

    The bank pair comes first and each count-difference pair after it, so the fund's
    register can pair every petty cash line with the line beside it.
    """
    label = ret.document_number or f"return {ret.pk}"
    lines = []
    if ret.amount:
        lines += [
            (ret.bank_account.gl_account, ret.amount, 0,
             f"Petty cash banked: {fund.name} ({label})"),
            (fund.gl_account, 0, ret.amount, f"Banked to {ret.bank_account.name} ({label})"),
        ]
    if ret.shortage:
        lines += [
            (over_short, ret.shortage, 0, f"Petty cash count short: {fund.name} ({label})"),
            (fund.gl_account, 0, ret.shortage, f"Count short ({label})"),
        ]
    if ret.overage:
        lines += [
            (fund.gl_account, ret.overage, 0, f"Count over ({label})"),
            (over_short, 0, ret.overage, f"Petty cash count over: {fund.name} ({label})"),
        ]
    return lines


@transaction.atomic
def _post_petty_cash_return_atomic(ret, *, actor_user=None):
    """Book the return on the fund's branch, set the new float and mark it POSTED.

    The fund row is locked first, then the count is held against the books: if a
    voucher, top-up or float change has landed since the tin was counted, the
    figures counted no longer describe the tin and the return is refused, to be
    counted again. A closure marks the fund closed on the return's date, by the
    person who raised it.
    """
    from .account_mappings import resolve_mapped_account
    from .models import JournalEntry, JournalLine, PettyCashFund, PettyCashReturn

    ret = PettyCashReturn.objects.select_for_update().get(pk=ret.pk)
    if ret.status not in (DocumentStatus.DRAFT, DocumentStatus.APPROVED):
        raise PettyCashError(
            f"Petty cash return {ret.document_number or ret.pk} is {state_word(ret)}; "
            f"only a draft or approved one can be posted.",
        )
    fund = PettyCashFund.objects.select_for_update().get(pk=ret.fund_id)
    ret.fund = fund
    validate_petty_cash_return(ret)
    on_hand = gl_cash_on_hand(fund)
    if on_hand != int(ret.book_balance):
        raise PettyCashError(
            f"Petty cash fund '{fund.name}' has moved since the count: the books said "
            f"{format_naira(ret.book_balance)} then and say {format_naira(on_hand)} now. "
            f"Count the tin again.",
        )
    if fund.float_amount != int(ret.previous_float_amount):
        raise PettyCashError(
            f"The float of petty cash fund '{fund.name}' changed since the count. "
            f"Count the tin again.",
        )

    over_short = (
        resolve_mapped_account(ret.entity, AccountMappingKey.CASH_OVER_SHORT)
        if ret.difference else None
    )
    lines = _return_lines(ret, fund, over_short)
    entry = None
    if lines:
        entry = JournalEntry.objects.create(
            entity=ret.entity, branch=ret.branch, date=ret.return_date,
            period=resolve_period(ret.entity, ret.return_date),
            source=JournalSource.BANK, currency=fund.currency,
            narration=ret.narration or f"{ret.get_kind_display()}: petty cash {fund.name}",
            reference=ret.reference or ret.document_number, created_by=actor_user,
        )
        for number, (account, debit, credit, description) in enumerate(lines, start=1):
            JournalLine.objects.create(
                entry=entry, account=account, debit=debit, credit=credit,
                description=description, line_no=number,
            )
        post_journal(entry, actor_user=actor_user)

    closing = ret.kind == PettyCashReturnKind.CLOSE
    fund.float_amount = int(ret.new_float_amount)
    fund.current_balance = gl_cash_on_hand(fund)
    fields = ["float_amount", "current_balance", "updated_at"]
    if closing:
        fund.is_active = False
        fund.closed_on = ret.return_date
        fund.closed_by = ret.created_by or actor_user
        fields += ["is_active", "closed_on", "closed_by"]
    fund.save(update_fields=fields)

    ret.journal = entry
    ret.status = DocumentStatus.POSTED
    ret.save(update_fields=["journal", "status", "updated_at"])

    record(
        entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_RETURN_POSTED,
        actor_user=actor_user, target=ret,
        message=(
            f"Banked {format_naira(ret.amount)} from petty cash '{fund.name}' "
            f"({ret.get_kind_display().lower()}); counted {format_naira(ret.counted_amount)} "
            f"against {format_naira(ret.book_balance)} on the books."
        ),
        journal_id=entry.pk if entry else None, kind=ret.kind, fund_id=fund.pk,
        amount=int(ret.amount), counted=int(ret.counted_amount),
        book_balance=int(ret.book_balance), shortage=ret.shortage, overage=ret.overage,
        bank_account_id=ret.bank_account_id, new_float=int(ret.new_float_amount),
    )
    if closing:
        record(
            entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_FUND_CLOSED,
            actor_user=actor_user, target=fund,
            message=(
                f"Closed petty cash fund '{fund.name}' on "
                f"{format_date(ret.return_date, ret.entity.tenant)} "
                f"with return {ret.document_number}."
            ),
            return_id=ret.pk, closed_on=ret.return_date.isoformat(),
        )
    return ret


def void_petty_cash_return(ret, *, actor_user=None, date=None):
    """Void a return, recording a durable rejection on failure."""
    try:
        return _void_petty_cash_return_atomic(ret, actor_user=actor_user, date=date)
    except FinanceError as exc:
        record_rejection(
            entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_RETURN_VOIDED,
            exc=exc, actor_user=actor_user, target=ret,
        )
        raise


@transaction.atomic
def _void_petty_cash_return_atomic(ret, *, actor_user=None, date=None):
    """Undo a return: cancel a draft, or reverse a posted one and restore the fund.

    A draft whose approval request is still open, returned to its sender
    included, is refused: the request is withdrawn first
    (:func:`vs_finance.approvals.refuse_while_request_open`), which itself
    cancels the return.

    A posted return is reversed as every bank document is: refused while its bank
    line is matched to a statement line (the bank says the money arrived, so the
    match is undone on the reconciliation first), and in a period that is still
    open. Reversing it puts the cash back in the tin's books and the float back
    where it was, and a closure's reversal reopens the fund.

    It is refused when the fund has moved on since: a later return of the fund is
    still standing, the float has been changed again, or a closed fund has been
    reopened. Undoing it then would restore a float or a closure nobody now
    expects, so the later change is undone first.
    """
    from .approvals import refuse_while_request_open
    from .banking import journal_is_reconciled
    from .models import PettyCashFund, PettyCashReturn
    from .posting import reverse_journal

    ret = PettyCashReturn.objects.select_for_update().get(pk=ret.pk)
    label = ret.document_number or ret.pk
    if ret.status == DocumentStatus.DRAFT:
        refuse_while_request_open(
            ret, noun="petty cash return",
            remedy="Withdraw the approval request instead: that cancels the return.",
        )
        ret.status = DocumentStatus.CANCELLED
        ret.save(update_fields=["status", "updated_at"])
        record(
            entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_RETURN_VOIDED,
            actor_user=actor_user, target=ret,
            message=f"Cancelled draft petty cash return {label}.",
        )
        return ret
    if ret.status != DocumentStatus.POSTED:
        raise PettyCashError(
            f"Petty cash return {label} is {state_word(ret)}. Only a posted return is "
            f"voided, or a draft cancelled; one waiting on approval is decided there.",
        )

    fund = PettyCashFund.objects.select_for_update().get(pk=ret.fund_id)
    later = (
        PettyCashReturn.objects.filter(fund_id=fund.pk, status=DocumentStatus.POSTED)
        .filter(pk__gt=ret.pk).values_list("document_number", flat=True).first()
    )
    if later is not None:
        raise PettyCashError(f"Return {later} of this fund came after {label}. Void it first.")
    closing = ret.kind == PettyCashReturnKind.CLOSE
    if closing and not fund.is_closed:
        raise PettyCashError(
            f"Petty cash fund '{fund.name}' has been reopened since {label} closed it.",
        )
    if fund.float_amount != int(ret.new_float_amount):
        raise PettyCashError(
            f"The float of petty cash fund '{fund.name}' has changed since {label}. "
            f"Put it back to {format_naira(ret.new_float_amount)} first.",
        )
    if ret.journal_id and journal_is_reconciled(ret.journal_id):
        raise PettyCashError(
            f"Petty cash return {label} is matched to a bank statement line. "
            f"Unmatch it on the reconciliation first.",
        )

    reversal = None
    if ret.journal_id:
        reversal = reverse_journal(
            ret.journal, actor_user=actor_user, date=date, document_owner=ret,
        )
    fund.float_amount = int(ret.previous_float_amount)
    fund.current_balance = gl_cash_on_hand(fund)
    fields = ["float_amount", "current_balance", "updated_at"]
    if closing:
        fund.is_active = True
        fund.closed_on = None
        fund.closed_by = None
        fields += ["is_active", "closed_on", "closed_by"]
    fund.save(update_fields=fields)

    ret.status = DocumentStatus.REVERSED
    ret.save(update_fields=["status", "updated_at"])
    record(
        entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_RETURN_VOIDED,
        actor_user=actor_user, target=ret,
        message=(
            f"Voided petty cash return {label}; {format_naira(ret.amount)} back in "
            f"'{fund.name}' and its float back to {format_naira(ret.previous_float_amount)}."
        ),
        journal_id=ret.journal_id, reversal_id=reversal.pk if reversal else None,
    )
    if closing:
        record(
            entity=ret.entity, action=FinanceAuditAction.PETTY_CASH_FUND_REOPENED,
            actor_user=actor_user, target=fund,
            message=f"Reopened petty cash fund '{fund.name}' by voiding its closure {label}.",
            return_id=ret.pk,
        )
    return ret


@transaction.atomic
def reopen_fund(fund, *, reason, float_amount=None, actor_user=None):
    """Bring a closed fund back into use, with a reason on the record.

    Reopening moves no cash: the closure banked it all, so the fund comes back empty
    and is funded again through :func:`establish_fund`. ``float_amount``, when
    given, is the float it runs on from now. The closure itself stays posted; to
    undo the closure's banking, void the closing return instead.
    """
    from .models import PettyCashFund

    fund = PettyCashFund.objects.select_for_update().get(pk=fund.pk)
    if not fund.is_closed:
        raise PettyCashError(f"Petty cash fund '{fund.name}' is not closed.")
    reason = (reason or "").strip()
    if not reason:
        raise PettyCashError("Say why the fund is being reopened.")
    closed_on, closed_by_id = fund.closed_on, fund.closed_by_id
    before_float = fund.float_amount
    fund.is_active = True
    fund.closed_on = None
    fund.closed_by = None
    if float_amount is not None:
        fund.float_amount = int(float_amount)
    fund.save(update_fields=["is_active", "closed_on", "closed_by", "float_amount", "updated_at"])
    record(
        entity=fund.entity, action=FinanceAuditAction.PETTY_CASH_FUND_REOPENED,
        actor_user=actor_user, target=fund,
        message=(
            f"Reopened petty cash fund '{fund.name}', closed on "
            f"{format_date(closed_on, fund.entity.tenant)}: {reason}"
        ),
        reason=reason, closed_on=closed_on.isoformat(), closed_by_id=closed_by_id,
        before={"float_amount": before_float}, after={"float_amount": fund.float_amount},
    )
    return fund


# --------------------------------------------------------------------------- #
# Read-only status (low-balance / replenishment alerts)                        #
# --------------------------------------------------------------------------- #

# Return active petty-cash fund status rows.
def fund_status(entity, *, threshold_bps=2500) -> list:
    """Per-fund cash position with a low-balance flag for replenishment alerts.

    ``threshold_bps`` is the fraction of the float (in basis points; default 25%) at or
    below which a fund is flagged ``needs_replenish``. Returns one dict per active fund.
    """
    from .models import PettyCashFund

    rows = []  # Response rows.
    qs = (  # Active funds for this entity.
        PettyCashFund.objects
        .filter(entity=entity, is_active=True)
        .select_related("gl_account")
        .order_by("name")
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
