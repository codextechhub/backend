"""The Stock & receiving view of the Procurement dashboard.

The third tab, for whoever keeps the stores: what stock is worth and where,
what is running low and how long it will last, what is due in, what has gone
out and to whom, what moved last, and four control figures (how fast stock
turns, how much arrives short or rejected, adjustments in the window, and goods
received that are still waiting for a bill).

Stores and documents
    Stock figures answer for the stores the reader works in: their branches'
    stores and the stores that belong to the whole school, as the stock screens
    read them. Receipts and orders are documents and answer under the reader's
    branches, as everywhere else in procurement.

Running low
    An item is low when its on-hand quantity is at or below its reorder level.
    Days left is on-hand divided by the average daily quantity issued over the
    last 90 days, or over the days since the item first moved when that is
    shorter, so a newly stocked item is not averaged over days it did not exist;
    an item never issued has no days-left figure. The suggested
    order is the item's reorder quantity, or at least enough to reach the
    reorder level again. :func:`low_stock` is what both this tab and the
    "draft a requisition" action read, so the table and the draft never differ.

Issued to
    Issues in the window by the cost centre named on each issue, untagged issues
    as their own row, at the cost they left stock at.
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from django.db.models import Count, Max, Min, Q, Sum

from vs_finance.constants import DocumentStatus

from .constants import StockMovementType
from .dashboard import _money

USAGE_DAYS = 90
IDLE_DAYS = 90
LOW_ROWS = 8
EXPECTED_DAYS = 14
EXPECTED_ROWS = 6
MOVEMENT_ROWS = 6
UNBILLED_DAYS = 30


def _bq(scope, prefix: str = "") -> Q:
    return scope.q(prefix) if scope is not None else Q()


def _num(value) -> float:
    return float(value or 0)


# --------------------------------------------------------------------------- #
# Running low                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class LowItem:
    item: object
    on_hand: Decimal
    store: str | None
    stores: int
    days_left: int | None
    suggested: Decimal
    unit_cost: int


def _unit_cost(item, on_hand: Decimal, value: int, last_receipt: dict) -> int:
    """What one unit costs now: average cost on hand, else the last receipt, else the catalogue."""
    if on_hand > 0 and value > 0:
        return int(value / on_hand)
    if item.id in last_receipt:
        return last_receipt[item.id]
    catalogue = getattr(item, "catalog_item", None)
    return int(getattr(catalogue, "standard_unit_price", 0) or 0)


def low_stock(entity, as_of, store_scope=None) -> list[LowItem]:
    """Items at or below their reorder level in the reader's stores, most urgent first."""
    from .models import StockBalance, StockItem, StockMovement

    balances = (
        StockBalance.objects.filter(stock_item__entity=entity, stock_item__is_active=True,
                                    location__is_active=True)
        .filter(_bq(store_scope, "location__")).select_related("location")
    )
    by_item = defaultdict(lambda: {"qty": Decimal(0), "value": 0, "stores": []})
    for b in balances:
        slot = by_item[b.stock_item_id]
        slot["qty"] += Decimal(b.on_hand_qty)
        slot["value"] += int(b.stock_value)
        slot["stores"].append((Decimal(b.on_hand_qty), b.location.name))
    items = StockItem.objects.filter(entity=entity, is_active=True, reorder_level__gt=0,
                                     pk__in=list(by_item)).select_related("catalog_item")
    since = as_of - datetime.timedelta(days=USAGE_DAYS - 1)
    issued = {
        r["stock_item_id"]: -Decimal(r["qty"] or 0)
        for r in StockMovement.objects.filter(
            entity=entity, movement_type=StockMovementType.ISSUE,
            movement_date__gte=since, movement_date__lte=as_of,
        ).filter(_bq(store_scope, "location__")).values("stock_item_id").annotate(qty=Sum("quantity"))
    }
    first_moved = dict(
        StockMovement.objects.filter(entity=entity, stock_item_id__in=list(by_item))
        .filter(_bq(store_scope, "location__")).values("stock_item_id")
        .annotate(first=Min("movement_date")).values_list("stock_item_id", "first")
    )
    last_receipt = {}
    for m in StockMovement.objects.filter(
        entity=entity, movement_type=StockMovementType.RECEIPT, quantity__gt=0,
    ).order_by("stock_item_id", "-movement_date", "-id").values("stock_item_id", "quantity", "value_amount"):
        last_receipt.setdefault(m["stock_item_id"], int(m["value_amount"] / Decimal(m["quantity"])))
    out = []
    for item in items:
        slot = by_item[item.id]
        on_hand, level = slot["qty"], Decimal(item.reorder_level)
        if on_hand > level:
            continue
        first = first_moved.get(item.id)
        span = min(USAGE_DAYS, (as_of - first).days + 1) if first else USAGE_DAYS
        per_day = issued.get(item.id, Decimal(0)) / max(span, 1)
        days = 0 if on_hand <= 0 else (int(on_hand / per_day) if per_day > 0 else None)
        gap = (level - on_hand).to_integral_value(rounding=ROUND_CEILING)
        suggested = max(Decimal(item.reorder_qty or 0), gap, Decimal(1))
        stores = sorted(slot["stores"], reverse=True)
        out.append(LowItem(
            item=item, on_hand=on_hand, store=stores[0][1] if stores else None, stores=len(stores),
            days_left=days, suggested=suggested, unit_cost=_unit_cost(item, on_hand, slot["value"], last_receipt),
        ))
    return sorted(out, key=lambda x: (x.days_left if x.days_left is not None else 10**6, x.item.name))


# --------------------------------------------------------------------------- #
# Stock blocks                                                                #
# --------------------------------------------------------------------------- #

def stock_position(entity, as_of, store_scope, low) -> dict:
    """What stock is worth, how many items are low or out, and what has not moved.

    Counted as the stock screen counts them: an item below its reorder level
    still holds something, and an item out of stock holds nothing in the
    reader's stores, so the two figures never include the same item.
    """
    from .models import StockBalance, StockLocation, StockMovement

    balances = StockBalance.objects.filter(stock_item__entity=entity, stock_item__is_active=True).filter(
        _bq(store_scope, "location__"))
    agg = balances.aggregate(value=Sum("stock_value"), items=Count("stock_item_id", distinct=True,
                                                                    filter=Q(on_hand_qty__gt=0)))
    stores = StockLocation.objects.filter(entity=entity, is_active=True).filter(_bq(store_scope)).count()
    held = {r["stock_item_id"]: (r["qty"], r["value"]) for r in balances.values("stock_item_id")
            .annotate(qty=Sum("on_hand_qty"), value=Sum("stock_value")).filter(qty__gt=0)}
    last_moved = dict(
        StockMovement.objects.filter(entity=entity, stock_item_id__in=list(held)).filter(_bq(store_scope, "location__"))
        .values("stock_item_id").annotate(last=Max("movement_date")).values_list("stock_item_id", "last")
    )
    idle_before = as_of - datetime.timedelta(days=IDLE_DAYS)
    idle = [pk for pk in held if last_moved.get(pk) is None or last_moved[pk] < idle_before]
    # The stock screen's reading: low still holds something; out holds nothing.
    below = [x for x in low if x.on_hand > 0]
    out = list(
        balances.values("stock_item_id", "stock_item__name").annotate(qty=Sum("on_hand_qty"))
        .filter(qty__lte=0).order_by("stock_item__name")
    )
    return {
        "value": _money(agg["value"]), "items": agg["items"], "stores": stores,
        "below_reorder": len(below),
        "out_within_week": sum(1 for x in below if x.days_left is not None and x.days_left <= 7),
        "out_of_stock": len(out),
        "out_names": [row["stock_item__name"] for row in out[:2]],
        "idle": len(idle), "idle_value": _money(sum(int(held[pk][1] or 0) for pk in idle)),
    }


def running_low(low) -> list:
    return [
        {
            "id": x.item.id, "name": x.item.name, "unit": x.item.unit_of_measure,
            "store": x.store, "stores": x.stores,
            "on_hand": _num(x.on_hand), "reorder_level": _num(x.item.reorder_level),
            "days_left": x.days_left, "suggested": _num(x.suggested),
        }
        for x in low[:LOW_ROWS]
    ]


def by_store(entity, store_scope) -> list:
    from .models import StockBalance

    rows = (
        StockBalance.objects.filter(stock_item__entity=entity, location__is_active=True)
        .filter(_bq(store_scope, "location__"))
        .values("location_id", "location__name").annotate(value=Sum("stock_value")).order_by("-value")
    )
    return [{"store": r["location__name"], "value": _money(r["value"])} for r in rows if r["value"]]


def issued_to(entity, start, end, store_scope) -> dict:
    """Stock issued in the window, by who it went to."""
    from .models import StockMovement

    rows = (
        StockMovement.objects.filter(entity=entity, movement_type=StockMovementType.ISSUE,
                                     movement_date__gte=start, movement_date__lte=end)
        .filter(_bq(store_scope, "location__"))
        .values("cost_center_id", "cost_center__name").annotate(value=Sum("value_amount"))
    )
    items = sorted(({"name": r["cost_center__name"], "value": _money(-int(r["value"] or 0))} for r in rows),
                   key=lambda r: (r["name"] is None, -r["value"]["kobo"]))
    return {"total": _money(sum(i["value"]["kobo"] for i in items)), "items": items}


def recent_movements(entity, store_scope) -> list:
    from .models import StockMovement

    rows = (
        StockMovement.objects.filter(entity=entity).filter(_bq(store_scope, "location__"))
        .select_related("stock_item", "location", "grn", "cost_center").order_by("-created_at", "-id")[:MOVEMENT_ROWS]
    )
    return [
        {
            "id": m.id, "type": m.movement_type, "item": m.stock_item.name, "unit": m.stock_item.unit_of_measure,
            "store": m.location.name if m.location_id else None,
            "reference": (m.grn.document_number if m.grn_id else "") or m.reference,
            "cost_center": m.cost_center.name if m.cost_center_id else None,
            "quantity": _num(m.quantity), "occurred_at": m.created_at.isoformat(),
        }
        for m in rows
    ]


def turns(entity, start, end, store_scope) -> dict | None:
    """Cost issued in the window over the average of opening and closing stock value."""
    from .models import StockBalance, StockMovement

    movements = StockMovement.objects.filter(entity=entity).filter(_bq(store_scope, "location__"))
    closing = int(StockBalance.objects.filter(stock_item__entity=entity).filter(_bq(store_scope, "location__"))
                  .aggregate(v=Sum("stock_value"))["v"] or 0)
    since = movements.filter(movement_date__gte=start).aggregate(v=Sum("value_amount"))["v"] or 0
    opening = closing - int(since)
    average = (opening + closing) / 2
    issued = -int(movements.filter(movement_type=StockMovementType.ISSUE, movement_date__gte=start,
                                   movement_date__lte=end).aggregate(v=Sum("value_amount"))["v"] or 0)
    # Each store by the same measure: issued over the average of its opening and closing value.
    per_store = defaultdict(lambda: {"issued": 0, "closing": 0, "since": 0})
    for r in (movements.filter(movement_type=StockMovementType.ISSUE, movement_date__gte=start, movement_date__lte=end)
              .values("location__name").annotate(v=Sum("value_amount"))):
        per_store[r["location__name"]]["issued"] = -int(r["v"] or 0)
    for r in movements.filter(movement_date__gte=start).values("location__name").annotate(v=Sum("value_amount")):
        per_store[r["location__name"]]["since"] = int(r["v"] or 0)
    for r in (StockBalance.objects.filter(stock_item__entity=entity).filter(_bq(store_scope, "location__"))
              .values("location__name").annotate(v=Sum("stock_value"))):
        per_store[r["location__name"]]["closing"] = int(r["v"] or 0)
    rates = []
    for name, f in per_store.items():
        held = (2 * f["closing"] - f["since"]) / 2
        if held > 0 and f["issued"] > 0:
            rates.append((name, f["issued"] / held))
    fastest = max(rates, key=lambda kv: kv[1], default=None)
    return {
        "times": round(issued / average, 1) if average > 0 else None,
        "fastest_store": fastest[0] if fastest else None,
        "fastest_times": round(fastest[1], 1) if fastest else None,
    }


def adjustments(entity, start, end, store_scope) -> dict:
    from .models import StockMovement

    agg = StockMovement.objects.filter(
        entity=entity, movement_type=StockMovementType.ADJUSTMENT, movement_date__gte=start, movement_date__lte=end,
    ).filter(_bq(store_scope, "location__")).aggregate(n=Count("id"), value=Sum("value_amount"))
    return {"count": agg["n"], "value": _money(agg["value"])}


# --------------------------------------------------------------------------- #
# Receiving                                                                   #
# --------------------------------------------------------------------------- #

def _short(line) -> bool:
    return _num(line["rejected_qty"]) > 0 or (
        line["expected_qty"] is not None and _num(line["accepted_qty"]) < _num(line["expected_qty"]))


def receipts(entity, start, end, as_of, doc_scope) -> dict:
    """Deliveries this week, and the share of received lines that came short or rejected."""
    from .models import GoodsReceivedNoteLine

    lines = GoodsReceivedNoteLine.objects.filter(
        grn__entity=entity, grn__status=DocumentStatus.POSTED,
    ).filter(_bq(doc_scope, "grn__"))
    week_start = as_of - datetime.timedelta(days=6)
    week = list(lines.filter(grn__received_date__gte=week_start, grn__received_date__lte=as_of)
                .values("grn_id", "accepted_qty", "rejected_qty", "expected_qty"))
    window = list(lines.filter(grn__received_date__gte=start, grn__received_date__lte=end)
                  .values("accepted_qty", "rejected_qty", "expected_qty"))
    short_lines = sum(1 for ln in window if _short(ln))
    return {
        "this_week": len({ln["grn_id"] for ln in week}),
        "this_week_short": len({ln["grn_id"] for ln in week if _short(ln)}),
        "short_lines": short_lines, "lines": len(window),
        "short_pct": round(short_lines * 100 / len(window), 1) if window else None,
    }


def unbilled(entity, as_of, doc_scope) -> dict:
    from .reports import grir_aging

    rows = [r for r in grir_aging(entity, as_of=as_of, branch_scope=doc_scope).rows if r.open_value > 0]
    return {"amount": _money(sum(r.open_value for r in rows)), "count": len(rows),
            "older": sum(1 for r in rows if r.days > UNBILLED_DAYS), "days": UNBILLED_DAYS}


def expected(entity, as_of, doc_scope) -> list:
    """Orders not yet fully received that are due within two weeks, or already late."""
    from .models import PurchaseOrder
    from .purchasing import po_receipt_stage

    orders = (
        PurchaseOrder.objects.filter(entity=entity, expected_date__isnull=False,
                                     expected_date__lte=as_of + datetime.timedelta(days=EXPECTED_DAYS))
        .filter(_bq(doc_scope))
        .exclude(status__in=(DocumentStatus.CANCELLED, DocumentStatus.REVERSED))
        # A draft counts only once it is in approval; an unsent draft is not on its way.
        .exclude(Q(status=DocumentStatus.DRAFT) & ~Q(approval_state="PENDING"))
        .select_related("vendor").prefetch_related("lines")
        .annotate(ordered=Sum("lines__quantity"), received=Sum("lines__received_qty"))
        .order_by("expected_date", "id")
    )
    out = []
    for po in orders:
        if po_receipt_stage(po.ordered, po.received) == "RECEIVED":
            continue
        days = (po.expected_date - as_of).days
        pending = po.approval_state == "PENDING" or po.status == DocumentStatus.PENDING_APPROVAL
        state = "awaiting_approval" if pending else ("late" if days < 0 else "due")
        first = min(po.lines.all(), key=lambda ln: (ln.line_no, ln.id), default=None)
        out.append({
            "id": po.id, "number": po.document_number or str(po.pk), "vendor": po.vendor.name,
            "title": first.description if first else "", "expected_date": po.expected_date.isoformat(),
            "days": days, "state": state, "partial": po_receipt_stage(po.ordered, po.received) == "PARTIAL",
        })
        if len(out) >= EXPECTED_ROWS:
            break
    return out


# --------------------------------------------------------------------------- #
# The view                                                                    #
# --------------------------------------------------------------------------- #

def stock_view(entity, *, user=None, as_of=None, doc_scope=None, store_scope=None, reader=None, window=None) -> dict:
    """The Stock & receiving payload for ``entity`` as ``reader`` may see it."""
    from django.utils import timezone

    from vs_finance.dashboard import EVERY_BLOCK, _current_period
    from vs_finance.dashboard_blocks import books_kind, resolve_window
    from vs_finance.dashboard_spend import window_days

    reader = reader or EVERY_BLOCK
    as_of = as_of or timezone.localdate()
    chosen, windows = resolve_window(entity, as_of, _current_period(entity, None), window)
    start, end = window_days(chosen, as_of)
    stock = reader.can("procurement.stock.view")
    grn = reader.can("procurement.goods_receipt.view")
    low = low_stock(entity, as_of, store_scope) if stock else []
    narrowed = any(s is not None and s.is_narrowed for s in (doc_scope, store_scope))
    return {
        "entity": entity.code,
        "currency": entity.base_currency_id,
        "books": books_kind(entity),
        "reader_first_name": (getattr(user, "first_name", "") or "").strip() or None,
        "as_of": as_of.isoformat(),
        "narrowed": narrowed,
        "window": {**chosen.payload(), "basis": "dates"},
        "windows": [{"key": w.key, "label": w.label, "name": w.name} for w in windows],
        "position": stock_position(entity, as_of, store_scope, low) if stock else None,
        "running_low": running_low(low) if stock else None,
        "by_store": by_store(entity, store_scope) if stock else None,
        "issued": issued_to(entity, start, end, store_scope) if stock else None,
        "movements": recent_movements(entity, store_scope) if stock else None,
        "turns": turns(entity, start, end, store_scope) if stock else None,
        "adjustments": adjustments(entity, start, end, store_scope) if stock else None,
        "receipts": receipts(entity, start, end, as_of, doc_scope) if grn else None,
        "unbilled": unbilled(entity, as_of, doc_scope) if grn else None,
        "expected": expected(entity, as_of, doc_scope) if reader.can("procurement.purchase_order.view") else None,
    }


# --------------------------------------------------------------------------- #
# Draft a requisition for what is low                                         #
# --------------------------------------------------------------------------- #

def draft_restock_requisition(entity, *, as_of, store_scope, branch, user, item_ids=None):
    """A DRAFT requisition with one line per low item at its suggested quantity.

    Nothing is submitted: the requisition goes through review and approval like
    any other. ``item_ids`` narrows the draft to some of the low items; items
    that are not low in the caller's stores are ignored, so the quantities are
    always the server's own.
    """
    from .models import PurchaseRequisition, PurchaseRequisitionLine
    from .settings import resolve_procurement_settings

    low = low_stock(entity, as_of, store_scope)
    if item_ids:
        wanted = {int(i) for i in item_ids}
        low = [x for x in low if x.item.id in wanted]
    if not low:
        return None
    lead = resolve_procurement_settings(entity).default_requisition_lead_days
    req = PurchaseRequisition.objects.create(
        entity=entity, branch=branch, title=f"Restock: {len(low)} item{'s' if len(low) != 1 else ''} below reorder level",
        request_date=as_of, needed_by=as_of + datetime.timedelta(days=lead) if lead else None,
        justification="Raised from the Stock & receiving dashboard for items at or below their reorder level.",
        requested_by=user, created_by=user,
    )
    for n, x in enumerate(low, start=1):
        catalogue = x.item.catalog_item
        PurchaseRequisitionLine.objects.create(
            requisition=req, line_no=n, catalog_item=catalogue, description=x.item.name,
            quantity=x.suggested, unit=(x.item.unit_of_measure or "Unit")[:24], estimated_unit_price=x.unit_cost,
            expense_account=x.item.default_expense_account or getattr(catalogue, "default_expense_account", None),
            tax_code=getattr(catalogue, "default_tax_code", None),
        )
    req.recompute_total(save=True)
    return req
