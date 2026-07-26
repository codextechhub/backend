"""Inventory / stock-ledger services — perpetual inventory at weighted-average cost.

The stock ledger keeps a :class:`~vs_procurement.models.StockItem`'s on-hand quantity
and GL value in lock-step. Valuation is **weighted-average held without floats**: the
item stores integer ``on_hand_qty`` and total ``stock_value`` (kobo); each movement
adjusts both atomically and snapshots the running balance, so ``stock_value`` always
equals the perpetual-inventory balance carried in the item's ``inventory_account``.

Three movement kinds touch the ledger:

* **receipt**  — :func:`receive_stock`, called from :func:`vs_procurement.purchasing.post_grn`
  for a stock-tracked GRN line. Raises qty/value at the purchase cost; the GRN journal
  (Dr inventory, Cr GR/IR) is what posts the GL side, so this only updates the sub-ledger.
* **issue**    — :func:`issue_stock`. Values the outflow at the current moving average and
  posts **Dr expense, Cr inventory**.
* **adjustment** — :func:`adjust_stock`. A signed stock-count / shrinkage / write-up
  correction, posting the value delta between inventory and an adjustment account.

:func:`reorder_report` and :func:`stock_valuation` are read-only views over the same state.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from vs_finance.audit import record, record_rejection
from vs_finance.constants import FinanceAuditAction, JournalSource
from vs_finance.exceptions import FinanceError, PostingError
from vs_finance.money import format_naira
from vs_finance.posting import post_journal, resolve_period

from .constants import INVENTORY_ADJUSTMENT_CODE, StockMovementType
from .exceptions import InsufficientStockError, StockError
from .purchasing import resolve_account


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _dec(value) -> Decimal:
    """Normalise model/input quantities without a binary-float round trip."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_stock_kobo(value) -> int:
    """Round a stock valuation once to integer kobo using explicit half-up semantics."""
    return int(_dec(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def lock_stock_items(stock_item_ids) -> dict:
    """Lock and return authoritative stock rows in stable primary-key order.

    The caller must own an outer transaction. Sorting the unique identifiers gives every
    multi-item receipt the same lock order, while the joined inventory account makes
    validation, valuation, and journal construction use the locked snapshots.
    """
    from .models import StockItem

    ids = sorted(set(stock_item_ids))
    return {
        item.pk: item
        for item in (
            StockItem.objects.select_for_update()
            # ``default_expense_account`` is nullable; joining it would put an outer-join
            # row under FOR UPDATE, which PostgreSQL rejects. The mandatory inventory
            # account is safe to join, while an issue can lazy-load its optional default
            # only after the authoritative StockItem row is locked.
            .select_related("inventory_account")
            .filter(pk__in=ids)
            .order_by("pk")
        )
    }


def _lock_stock_item(stock_item):
    """Re-read one caller-supplied stock instance under a row lock."""
    from .models import StockItem

    locked = lock_stock_items([stock_item.pk]).get(stock_item.pk)
    if locked is None:
        raise StockItem.DoesNotExist(
            f"StockItem matching query does not exist (pk={stock_item.pk})."
        )
    return locked


def _record_movement(stock_item, *, movement_type, quantity, value_amount,
                     movement_date, grn=None, journal=None, actor_user=None,
                     reference="", narration=""):
    """Apply a signed (qty, value) delta to ``stock_item`` and append a ledger row.

    Applies the signed quantity/value deltas to the item's running ``on_hand_qty`` /
    ``stock_value`` first, then snapshots those post-movement balances onto the immutable
    :class:`~vs_procurement.models.StockMovement`. Caller owns the transaction.
    """
    from .models import StockMovement

    stock_item.on_hand_qty = _dec(stock_item.on_hand_qty) + _dec(quantity)
    stock_item.stock_value = int(stock_item.stock_value) + int(value_amount)
    stock_item.save(update_fields=["on_hand_qty", "stock_value", "updated_at"])

    return StockMovement.objects.create(
        entity=stock_item.entity, stock_item=stock_item,
        movement_type=movement_type, movement_date=movement_date,
        quantity=_dec(quantity), value_amount=int(value_amount),
        balance_qty=stock_item.on_hand_qty, balance_value=stock_item.stock_value,
        grn=grn, journal=journal, created_by=actor_user,
        reference=reference, narration=narration,
    )


def _issue_value(stock_item, quantity: Decimal) -> int:
    """Weighted-average value (kobo) of issuing ``quantity`` — proportion of total value.

    Computed as ``stock_value × quantity / on_hand_qty`` and rounded once to integer
    kobo. When ``quantity == on_hand_qty`` the ratio returns the entire carried value,
    so exact depletion cannot strand a rounding residue in ``stock_value``. This avoids
    persisting a fractional unit cost between movements.
    """
    on_hand = _dec(stock_item.on_hand_qty)
    if on_hand <= 0:
        return 0
    if quantity == on_hand:
        return int(stock_item.stock_value)
    value = (Decimal(stock_item.stock_value) * quantity / on_hand)
    return round_stock_kobo(value)


# --------------------------------------------------------------------------- #
# Receipt (called from the GRN posting — GL side already booked there)        #
# --------------------------------------------------------------------------- #

@transaction.atomic
def receive_stock(stock_item, *, quantity, value, movement_date, grn=None,
                  journal=None, actor_user=None, reference="", narration=""):
    """Raise on-hand qty/value for a received stock line (weighted-average in).

    The GL entry (Dr inventory, Cr GR/IR) is posted by the GRN; this only updates the
    sub-ledger and writes the RECEIPT movement. ``value`` is the accepted ex-tax cost
    of ``quantity`` units, so the new average folds the purchase price in automatically.
    """
    quantity = _dec(quantity)
    if quantity <= 0:
        raise StockError("A stock receipt must have a positive quantity.")
    stock_item = _lock_stock_item(stock_item)
    return _record_movement(
        stock_item, movement_type=StockMovementType.RECEIPT,
        quantity=quantity, value_amount=int(value), movement_date=movement_date,
        grn=grn, journal=journal, actor_user=actor_user,
        reference=reference, narration=narration or "Goods received into stock",
    )


# --------------------------------------------------------------------------- #
# Issue (Dr expense, Cr inventory)                                            #
# --------------------------------------------------------------------------- #

def issue_stock(stock_item, *, quantity, movement_date, expense_account=None,
                actor_user=None, reference="", narration=""):
    """Issue ``quantity`` out of stock at moving-average cost (Dr expense, Cr inventory).

    Wrapper recording a durable rejection audit on any :class:`FinanceError`, then
    re-raising — mirroring the journal posting contract.
    """
    try:
        return _issue_stock_atomic(
            stock_item, quantity=quantity, movement_date=movement_date,
            expense_account=expense_account, actor_user=actor_user,
            reference=reference, narration=narration,
        )
    except FinanceError as exc:
        record_rejection(
            entity=stock_item.entity, action=FinanceAuditAction.STOCK_ISSUE_REJECTED,
            exc=exc, actor_user=actor_user, target=stock_item,
        )
        raise


@transaction.atomic
def _issue_stock_atomic(stock_item, *, quantity, movement_date, expense_account=None,
                        actor_user=None, reference="", narration=""):
    """Post inventory relief and append its signed movement in one transaction.

    The journal owns the GL effect (Dr expense, Cr inventory); the movement owns the
    perpetual-stock running balances. Any validation or posting failure rolls both back.
    """
    from vs_finance.models import JournalEntry, JournalLine

    stock_item = _lock_stock_item(stock_item)
    quantity = _dec(quantity)
    if quantity <= 0:
        raise StockError("A stock issue must have a positive quantity.")
    on_hand = _dec(stock_item.on_hand_qty)
    if quantity > on_hand:
        raise InsufficientStockError(
            item_code=stock_item.code, requested=quantity, on_hand=on_hand,
        )

    expense = expense_account or stock_item.default_expense_account
    if expense is None:
        raise StockError(
            f"Stock item '{stock_item.code}' has no expense account and none was given "
            f"for the issue.",
        )

    value = _issue_value(stock_item, quantity)
    if value <= 0:
        raise StockError("A stock issue must have a positive value to post.")

    inventory = stock_item.inventory_account
    period = resolve_period(stock_item.entity, movement_date)
    entry = JournalEntry.objects.create(
        entity=stock_item.entity, date=movement_date, period=period,
        source=JournalSource.PURCHASE,
        narration=narration or f"Stock issue: {stock_item.code}",
        reference=reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=expense, debit=value, credit=0,
        description=f"Stock issued: {stock_item.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=inventory, debit=0, credit=value,
        description=f"Inventory relief: {stock_item.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    # Outflows are stored as negative deltas, while balance_qty/balance_value snapshot
    # the state after applying them. This makes a movement independently auditable.
    movement = _record_movement(
        stock_item, movement_type=StockMovementType.ISSUE,
        quantity=-quantity, value_amount=-value, movement_date=movement_date,
        journal=entry, actor_user=actor_user, reference=reference,
        narration=narration or "Stock issued",
    )
    record(
        entity=stock_item.entity, action=FinanceAuditAction.STOCK_ISSUED,
        actor_user=actor_user, target=stock_item,
        message=f"Issued {quantity} of {stock_item.code} ({format_naira(value)} to expense).",
        journal_id=entry.pk, value=value,
    )
    return movement


# --------------------------------------------------------------------------- #
# Adjustment (signed correction between inventory and an adjustment account)   #
# --------------------------------------------------------------------------- #

def adjust_stock(stock_item, *, quantity_delta, movement_date, adjustment_account=None,
                 unit_cost=None, actor_user=None, reference="", narration=""):
    """Apply a signed stock-count correction (write-up if ``+``, shrinkage if ``−``).

    Wrapper recording a durable rejection audit on any :class:`FinanceError`, then
    re-raising.
    """
    try:
        return _adjust_stock_atomic(
            stock_item, quantity_delta=quantity_delta, movement_date=movement_date,
            adjustment_account=adjustment_account, unit_cost=unit_cost,
            actor_user=actor_user, reference=reference, narration=narration,
        )
    except FinanceError as exc:
        record_rejection(
            entity=stock_item.entity, action=FinanceAuditAction.STOCK_ADJUST_REJECTED,
            exc=exc, actor_user=actor_user, target=stock_item,
        )
        raise


@transaction.atomic
def _adjust_stock_atomic(stock_item, *, quantity_delta, movement_date,
                         adjustment_account=None, unit_cost=None, actor_user=None,
                         reference="", narration=""):
    """Value and post one signed correction, then snapshot the resulting stock state.

    Positive deltas write inventory up; negative deltas relieve inventory at its moving
    average. The GL journal and stock movement share this transaction, so neither side
    can survive without the other.
    """
    from vs_finance.models import JournalEntry, JournalLine

    stock_item = _lock_stock_item(stock_item)
    delta = _dec(quantity_delta)
    if delta == 0:
        raise StockError("A stock adjustment must change the quantity.")
    on_hand = _dec(stock_item.on_hand_qty)
    if delta < 0 and -delta > on_hand:
        raise InsufficientStockError(
            item_code=stock_item.code, requested=-delta, on_hand=on_hand,
        )

    # Value the change: a decrease relieves at the current average; an increase uses the
    # given unit cost, falling back to the current average when stock is already held.
    if delta < 0:
        value = _issue_value(stock_item, -delta)
    elif unit_cost is not None:
        value = round_stock_kobo(_dec(unit_cost) * delta)
    elif on_hand > 0:
        value = round_stock_kobo(Decimal(stock_item.stock_value) * delta / on_hand)
    else:
        raise StockError(
            "A unit_cost is required to increase stock that has no existing average cost.",
        )
    if value <= 0:
        raise StockError("A stock adjustment must have a positive value to post.")

    adj = adjustment_account or resolve_account(
        stock_item.entity, INVENTORY_ADJUSTMENT_CODE, label="Inventory adjustments",
    )
    inventory = stock_item.inventory_account
    period = resolve_period(stock_item.entity, movement_date)
    entry = JournalEntry.objects.create(
        entity=stock_item.entity, date=movement_date, period=period,
        source=JournalSource.PURCHASE,
        narration=narration or f"Stock adjustment: {stock_item.code}",
        reference=reference, created_by=actor_user,
    )
    if delta > 0:                       # write-up: Dr inventory, Cr adjustment
        debit_acc, credit_acc = inventory, adj
    else:                               # shrinkage: Dr adjustment, Cr inventory
        debit_acc, credit_acc = adj, inventory
    JournalLine.objects.create(
        entry=entry, account=debit_acc, debit=value, credit=0,
        description=f"Stock adjustment: {stock_item.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=credit_acc, debit=0, credit=value,
        description=f"Stock adjustment: {stock_item.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    # Journal lines are unsigned debit/credit amounts; the stock ledger instead records
    # direction explicitly, so shrinkage carries a negative quantity and value snapshot.
    signed_value = value if delta > 0 else -value
    movement = _record_movement(
        stock_item, movement_type=StockMovementType.ADJUSTMENT,
        quantity=delta, value_amount=signed_value, movement_date=movement_date,
        journal=entry, actor_user=actor_user, reference=reference,
        narration=narration or "Stock adjusted",
    )
    record(
        entity=stock_item.entity, action=FinanceAuditAction.STOCK_ADJUSTED,
        actor_user=actor_user, target=stock_item,
        message=f"Adjusted {stock_item.code} by {delta} ({format_naira(signed_value)}).",
        journal_id=entry.pk, value=value,
    )
    return movement


# --------------------------------------------------------------------------- #
# Read-only views over stock state                                            #
# --------------------------------------------------------------------------- #

def reorder_report(entity) -> list:
    """Active stock items at/below their reorder level, with a suggested order qty.

    The entity predicate is the tenant boundary; inactive catalogue rows are excluded
    from replenishment even though they remain available to historical movements.
    """
    from .models import StockItem

    rows = []
    qs = (
        StockItem.objects
        .filter(entity=entity, is_active=True)
        .select_related("inventory_account")
        .order_by("code")
    )
    for item in qs:
        if _dec(item.on_hand_qty) <= _dec(item.reorder_level):
            rows.append({
                "stock_item_id": item.id, "code": item.code, "name": item.name,
                "on_hand_qty": item.on_hand_qty, "reorder_level": item.reorder_level,
                "reorder_qty": item.reorder_qty, "unit_cost": item.unit_cost,
            })
    return rows


def stock_valuation(entity) -> dict:
    """On-hand value per entity item plus the grand total (ties to inventory GL).

    Unlike :func:`reorder_report`, valuation includes inactive items: deactivation does
    not erase stock already held or its control-account reconciliation obligation.
    """
    from .models import StockItem

    qs = StockItem.objects.filter(entity=entity).order_by("code")
    rows, total = [], 0
    for item in qs:
        total += int(item.stock_value)
        rows.append({
            "stock_item_id": item.id, "code": item.code, "name": item.name,
            "on_hand_qty": item.on_hand_qty, "unit_cost": item.unit_cost,
            "stock_value": int(item.stock_value),
        })
    return {"rows": rows, "total_value": total}
