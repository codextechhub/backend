"""Inventory / stock-ledger services - perpetual inventory at weighted-average cost.

The stock ledger keeps a :class:`~vs_procurement.models.StockItem`'s on-hand quantity
and GL value in lock-step. Valuation is **weighted-average held without floats**: each
movement adjusts quantity and value atomically and snapshots the running balance, so
the recorded value always equals the perpetual-inventory balance carried in the item's
``inventory_account``.

**Stock is held per location.** It used to be one pool per entity, which is wrong the
moment a school has two campuses: the pool knew a thousand units existed but not that
seven hundred sat at one site, so an issue at the other drew against stock it did not
have and the availability check allowed it. One blended average also meant a site that
bought dearer and a site that bought cheaper both issued at the middle.

:class:`~vs_procurement.models.StockBalance` now holds the quantity and value per
(item, location) and is the authority. The item's own ``on_hand_qty`` and
``stock_value`` are maintained as the **roll-up** across locations, so every report,
serializer and reorder rule that reads them keeps reading the same numbers.

A caller that names no location gets the entity's default, which is what lets a
single-location school keep calling these services unchanged. Once an entity has more
than one, a call that does not say where is refused: guessing which campus stock left
is the defect this exists to fix.

Three movement kinds touch the ledger:

* **receipt**  - :func:`receive_stock`, called from :func:`vs_procurement.purchasing.post_grn`
  for a stock-tracked GRN line. Raises qty/value at the purchase cost; the GRN journal
  (Dr inventory, Cr GR/IR) is what posts the GL side, so this only updates the sub-ledger.
* **issue**    - :func:`issue_stock`. Values the outflow at the current moving average and
  posts **Dr expense, Cr inventory**.
* **adjustment** - :func:`adjust_stock`. A signed stock-count / shrinkage / write-up
  correction, posting the value delta between inventory and an adjustment account.

"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction

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


def default_location(entity):
    """The entity's default stock location, or ``None`` when it has none."""
    from .models import StockLocation

    return StockLocation.objects.filter(
        entity=entity, is_default=True, is_active=True,
    ).first()


def resolve_location(entity, location=None):
    """Decide which location a movement applies to.

    With one active location the caller need not name it, which is how the dimension
    recedes for a school that has a single store. With more than one, a call that names
    none is refused rather than defaulted: silently drawing from the main store when
    somebody meant the annex is a quieter version of the bug locations exist to fix.
    """
    from .models import StockLocation

    if location is not None:
        if location.entity_id != entity.pk:
            raise StockError(
                f"Stock location '{location.code}' belongs to another entity.",
            )
        if not location.is_active:
            raise StockError(f"Stock location '{location.code}' is not active.")
        return location

    active = list(StockLocation.objects.filter(entity=entity, is_active=True)[:2])
    if not active:
        raise StockError(
            "This entity has no stock location. Create one before moving stock.",
        )
    if len(active) > 1:
        chosen = default_location(entity)
        raise StockError(
            "This entity has more than one stock location, so a movement must say "
            f"which one. Pass a location{f' (default: {chosen.code})' if chosen else ''}.",
        )
    return active[0]


def lock_balance(stock_item, location):
    """Lock (creating if absent) the balance row for one item at one location.

    Created on first use rather than up front for every pairing: an entity with forty
    items and six stores would otherwise carry two hundred and forty rows, nearly all
    of them permanently zero.
    """
    from .models import StockBalance

    StockBalance.objects.get_or_create(
        stock_item=stock_item, location=location,
        defaults={"on_hand_qty": 0, "stock_value": 0},
    )
    return StockBalance.objects.select_for_update().get(
        stock_item=stock_item, location=location,
    )


def location_for_branch(entity, branch):
    """The stock location a document's branch implies, or the entity's default.

    A goods receipt already knows where it landed: ``GoodsReceivedNote`` carries a
    branch through ``FinanceDocument``. Preferring that branch's own store means goods
    are received where they physically arrived without anybody having to say so twice.
    A branch with several stores resolves to its default, then to the entity's, which
    keeps a single-store school unaffected.
    """
    from .models import StockLocation

    if branch is not None:
        at_branch = StockLocation.objects.filter(
            entity=entity, branch=branch, is_active=True,
        ).order_by("-is_default", "code").first()
        if at_branch is not None:
            return at_branch
    return default_location(entity)


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


def _record_movement(stock_item, balance, *, movement_type, quantity, value_amount,
                     movement_date, grn=None, journal=None, actor_user=None,
                     reference="", narration=""):
    """Apply a signed (qty, value) delta at one location and append a ledger row.

    Two things move together and must not drift: the location's own balance, which is
    the authority, and the item's totals, which are the roll-up every existing report
    reads. Both are written here so no caller can update one and forget the other.

    The movement snapshots the **location's** post-movement balance, so the history
    reconstructs that site's position rather than a blend of every site. Caller owns
    the transaction.
    """
    from .models import StockMovement

    quantity = _dec(quantity)
    value_amount = int(value_amount)

    balance.on_hand_qty = _dec(balance.on_hand_qty) + quantity
    balance.stock_value = int(balance.stock_value) + value_amount
    balance.save(update_fields=["on_hand_qty", "stock_value", "updated_at"])

    # The roll-up moves by the same delta, which keeps it equal to the sum of the
    # balances without re-summing them on every movement.
    stock_item.on_hand_qty = _dec(stock_item.on_hand_qty) + quantity
    stock_item.stock_value = int(stock_item.stock_value) + value_amount
    stock_item.save(update_fields=["on_hand_qty", "stock_value", "updated_at"])

    return StockMovement.objects.create(
        entity=stock_item.entity, stock_item=stock_item, location=balance.location,
        movement_type=movement_type, movement_date=movement_date,
        quantity=quantity, value_amount=value_amount,
        balance_qty=balance.on_hand_qty, balance_value=balance.stock_value,
        grn=grn, journal=journal, created_by=actor_user,
        reference=reference, narration=narration,
    )


def _issue_value(balance, quantity: Decimal) -> int:
    """Weighted-average value (kobo) of issuing ``quantity`` from one location.

    Computed as ``stock_value × quantity / on_hand_qty`` **at that location** and
    rounded once to integer kobo, so a site that bought dearer relieves at its own cost
    rather than at a blend with every other site. When ``quantity == on_hand_qty`` the
    ratio returns the entire carried value, so exact depletion cannot strand a rounding
    residue. This avoids persisting a fractional unit cost between movements.
    """
    on_hand = _dec(balance.on_hand_qty)
    if on_hand <= 0:
        return 0
    if quantity == on_hand:
        return int(balance.stock_value)
    value = (Decimal(balance.stock_value) * quantity / on_hand)
    return round_stock_kobo(value)


# --------------------------------------------------------------------------- #
# Receipt (called from the GRN posting - GL side already booked there)        #
# --------------------------------------------------------------------------- #

@transaction.atomic
def receive_stock(stock_item, *, quantity, value, movement_date, location=None,
                  grn=None, journal=None, actor_user=None, reference="", narration=""):
    """Raise qty/value at one location for a received stock line (weighted-average in).

    The GL entry (Dr inventory, Cr GR/IR) is posted by the GRN; this only updates the
    sub-ledger and writes the RECEIPT movement. ``value`` is the accepted ex-tax cost
    of ``quantity`` units, so that location's average folds the purchase price in.

    ``location`` defaults through :func:`resolve_location`. A receipt normally passes
    the one derived from its GRN's branch, so goods land where they physically arrived.
    """
    quantity = _dec(quantity)
    if quantity <= 0:
        raise StockError("A stock receipt must have a positive quantity.")
    stock_item = _lock_stock_item(stock_item)
    location = resolve_location(stock_item.entity, location)
    balance = lock_balance(stock_item, location)
    movement = _record_movement(
        stock_item, balance, movement_type=StockMovementType.RECEIPT,
        quantity=quantity, value_amount=int(value), movement_date=movement_date,
        grn=grn, journal=journal, actor_user=actor_user,
        reference=reference, narration=narration or "Goods received into stock",
    )
    record(
        entity=stock_item.entity, action=FinanceAuditAction.STOCK_RECEIVED,
        actor_user=actor_user, target=stock_item,
        message=(
            f"Received {quantity} of {stock_item.code} into {location.code} "
            f"({format_naira(int(value))} into inventory)."
        ),
        movement_id=movement.pk, location_id=location.pk,
        journal_id=journal.pk if journal is not None else None,
        grn_id=grn.pk if grn is not None else None,
        value=int(value),
    )
    return movement


# --------------------------------------------------------------------------- #
# Issue (Dr expense, Cr inventory)                                            #
# --------------------------------------------------------------------------- #

def issue_stock(stock_item, *, quantity, movement_date, location=None,
                expense_account=None, actor_user=None, reference="", narration=""):
    """Issue ``quantity`` out of stock at moving-average cost (Dr expense, Cr inventory).

    Wrapper recording a durable rejection audit on any :class:`FinanceError`, then
    re-raising - mirroring the journal posting contract.
    """
    try:
        return _issue_stock_atomic(
            stock_item, quantity=quantity, movement_date=movement_date,
            location=location, expense_account=expense_account, actor_user=actor_user,
            reference=reference, narration=narration,
        )
    except FinanceError as exc:
        record_rejection(
            entity=stock_item.entity, action=FinanceAuditAction.STOCK_ISSUE_REJECTED,
            exc=exc, actor_user=actor_user, target=stock_item,
        )
        raise


@transaction.atomic
def _issue_stock_atomic(stock_item, *, quantity, movement_date, location=None,
                        expense_account=None, actor_user=None, reference="",
                        narration=""):
    """Post inventory relief and append its signed movement in one transaction.

    The journal owns the GL effect (Dr expense, Cr inventory); the movement owns the
    perpetual-stock running balances. Any validation or posting failure rolls both back.
    """
    from vs_finance.models import JournalEntry, JournalLine

    stock_item = _lock_stock_item(stock_item)
    quantity = _dec(quantity)
    if quantity <= 0:
        raise StockError("A stock issue must have a positive quantity.")
    location = resolve_location(stock_item.entity, location)
    balance = lock_balance(stock_item, location)
    # Availability is the location's, not the entity's. Checking the roll-up is what
    # let one campus issue against stock physically standing at another.
    on_hand = _dec(balance.on_hand_qty)
    if quantity > on_hand:
        raise InsufficientStockError(
            item_code=f"{stock_item.code}@{location.code}",
            requested=quantity, on_hand=on_hand,
        )

    expense = expense_account or stock_item.default_expense_account
    if expense is None:
        raise StockError(
            f"Stock item '{stock_item.code}' has no expense account and none was given "
            f"for the issue.",
        )

    value = _issue_value(balance, quantity)
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
        stock_item, balance, movement_type=StockMovementType.ISSUE,
        quantity=-quantity, value_amount=-value, movement_date=movement_date,
        journal=entry, actor_user=actor_user, reference=reference,
        narration=narration or "Stock issued",
    )
    record(
        entity=stock_item.entity, action=FinanceAuditAction.STOCK_ISSUED,
        actor_user=actor_user, target=stock_item,
        message=(
            f"Issued {quantity} of {stock_item.code} from {location.code} "
            f"({format_naira(value)} to expense)."
        ),
        journal_id=entry.pk, value=value, location_id=location.pk,
    )
    return movement


# --------------------------------------------------------------------------- #
# Adjustment (signed correction between inventory and an adjustment account)   #
# --------------------------------------------------------------------------- #

def adjust_stock(stock_item, *, quantity_delta, movement_date, location=None,
                 adjustment_account=None, unit_cost=None, actor_user=None,
                 reference="", narration=""):
    """Apply a signed stock-count correction (write-up if ``+``, shrinkage if ``−``).

    Wrapper recording a durable rejection audit on any :class:`FinanceError`, then
    re-raising.
    """
    try:
        return _adjust_stock_atomic(
            stock_item, quantity_delta=quantity_delta, movement_date=movement_date,
            location=location, adjustment_account=adjustment_account,
            unit_cost=unit_cost, actor_user=actor_user, reference=reference,
            narration=narration,
        )
    except FinanceError as exc:
        record_rejection(
            entity=stock_item.entity, action=FinanceAuditAction.STOCK_ADJUST_REJECTED,
            exc=exc, actor_user=actor_user, target=stock_item,
        )
        raise


@transaction.atomic
def _adjust_stock_atomic(stock_item, *, quantity_delta, movement_date, location=None,
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
    location = resolve_location(stock_item.entity, location)
    balance = lock_balance(stock_item, location)
    # A count corrects one shelf, so it is measured against that shelf's balance.
    on_hand = _dec(balance.on_hand_qty)
    if delta < 0 and -delta > on_hand:
        raise InsufficientStockError(
            item_code=f"{stock_item.code}@{location.code}",
            requested=-delta, on_hand=on_hand,
        )

    # Value the change: a decrease relieves at the current average; an increase uses the
    # given unit cost, falling back to the current average when stock is already held.
    if delta < 0:
        value = _issue_value(balance, -delta)
    elif unit_cost is not None:
        value = round_stock_kobo(_dec(unit_cost) * delta)
    elif on_hand > 0:
        value = round_stock_kobo(Decimal(balance.stock_value) * delta / on_hand)
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
        stock_item, balance, movement_type=StockMovementType.ADJUSTMENT,
        quantity=delta, value_amount=signed_value, movement_date=movement_date,
        journal=entry, actor_user=actor_user, reference=reference,
        narration=narration or "Stock adjusted",
    )
    record(
        entity=stock_item.entity, action=FinanceAuditAction.STOCK_ADJUSTED,
        actor_user=actor_user, target=stock_item,
        message=(
            f"Adjusted {stock_item.code} at {location.code} by {delta} "
            f"({format_naira(signed_value)})."
        ),
        journal_id=entry.pk, value=value, location_id=location.pk,
    )
    return movement
