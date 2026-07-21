"""Stock items and movements.
"""
from __future__ import annotations

import datetime

from django.db.models import Count, F, Prefetch, Q, Sum
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity

from .. import stock
from ..models import (
    StockItem,
    StockMovement,
)
from ..serializers import (
    StockItemDetailSerializer,
    StockItemListSerializer,
    StockMovementSerializer,
)


from .base import (
    _kobo,
    _ProcBase,
    _date,
    _nonneg_qty,
    _quantity,
    _resolve_asset_account,
    _resolve_expense_account,
    _signed_qty,
    _strict_kobo,
    _text,
)
from .catalog import _resolve_catalog_item

# --------------------------------------------------------------------------- #
# Inventory / stock ledger                                                     #
# --------------------------------------------------------------------------- #


def _stock_detail(entity, pk):
    """One stock item ready for the DETAIL serializer: accounts/catalog joined and the
    movement ledger prefetched (newest first, with its actor) so the drawer's Movements
    tab and ``created_by_name`` never fan out into per-row queries."""
    return (
        StockItem.objects
        .filter(entity=entity, pk=pk)
        .select_related("inventory_account", "default_expense_account", "catalog_item")
        .prefetch_related(Prefetch(
            "movements",
            queryset=StockMovement.objects.select_related("created_by").order_by("-id"),
        ))
        .first()
    )

class StockItemListCreateView(_ProcBase):
    """GET (list) / POST (create) stock items — perpetual-inventory masters.

    docstring-name: Stock items
    """

    @property
    def rbac_permission(self):
        return "procurement.stock.manage" if self.request.method == "POST" \
            else "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = StockItem.objects.filter(entity=entity).select_related(
            "inventory_account", "default_expense_account", "catalog_item")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("q")):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        if request.query_params.get("needs_reorder") == "true":
            qs = qs.filter(is_active=True, on_hand_qty__lte=F("reorder_level"))
        return self.paginate(request, qs.order_by("code"), StockItemListSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        # Codes are entity-unique identifiers — normalise to trimmed upper-case so the
        # uniqueness constraint and later immutability check compare like-for-like.
        code = _text(body.get("code"), "code", 40, required=True).upper()
        name = _text(body.get("name"), "name", 200, required=True)
        # Inventory account must be an active, postable ASSET account (required); the
        # default expense account (debited on issue) an active, postable EXPENSE (optional).
        inventory = _resolve_asset_account(
            entity, body.get("inventory_account"), "inventory_account")
        if inventory is None:
            raise ValidationError(
                {"inventory_account": "An inventory asset account is required."})
        item = StockItem.objects.create(
            entity=entity, code=code, name=name,
            description=_text(body.get("description", ""), "description", 255),
            unit_of_measure=body.get("unit_of_measure") or "each",
            catalog_item=_resolve_catalog_item(entity, body.get("catalog_item")),
            inventory_account=inventory,
            default_expense_account=_resolve_expense_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            reorder_level=_nonneg_qty(body.get("reorder_level", 0), "reorder_level"),
            reorder_qty=_nonneg_qty(body.get("reorder_qty", 0), "reorder_qty"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Stock item created.",
            data=StockItemDetailSerializer(_stock_detail(entity, item.pk)).data,
            status=201,
        )


class StockItemDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update master fields, not balances) one stock item.

    docstring-name: Stock items
    """

    @property
    def rbac_permission(self):
        return "procurement.stock.manage" if self.request.method == "PATCH" \
            else "procurement.stock.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        item = _stock_detail(entity, pk)
        if item is None:
            raise NotFound("No such stock item in this entity.")
        return success_response(
            "Stock item retrieved.", data=StockItemDetailSerializer(item).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        # The code is the item's stable identifier — sending a different one is an error,
        # never a silent rename (movements/valuation reference it). Compare on the same
        # trimmed upper-case normalisation used at create.
        if "code" in body:
            if _text(body.get("code"), "code", 40).upper() != item.code:
                raise ValidationError({"code": "Stock code cannot be changed."})
        if "name" in body:
            item.name = _text(body.get("name"), "name", 200, required=True)
        if "description" in body:
            item.description = _text(body.get("description", ""), "description", 255)
        if "unit_of_measure" in body:
            item.unit_of_measure = body["unit_of_measure"] or "each"
        if "catalog_item" in body:
            # Preserve an existing inactive historical link, but do not permit a new one.
            item.catalog_item = _resolve_catalog_item(
                entity, body.get("catalog_item"), current_id=item.catalog_item_id,
            )
        if "inventory_account" in body:
            # A changed inventory account must still be an active, postable ASSET account.
            inv = _resolve_asset_account(entity, body.get("inventory_account"), "inventory_account")
            if inv is None:
                raise ValidationError(
                    {"inventory_account": "An inventory asset account is required."})
            item.inventory_account = inv
        if "default_expense_account" in body:
            # Active, postable EXPENSE (or cleared to None).
            item.default_expense_account = _resolve_expense_account(
                entity, body.get("default_expense_account"), "default_expense_account")
        if "reorder_level" in body:
            item.reorder_level = _nonneg_qty(body.get("reorder_level", 0), "reorder_level")
        if "reorder_qty" in body:
            item.reorder_qty = _nonneg_qty(body.get("reorder_qty", 0), "reorder_qty")
        if "is_active" in body:
            item.is_active = bool(body["is_active"])
        # NB: on_hand_qty / stock_value are ledger-owned and deliberately never patchable here.
        item.save()
        return success_response(
            "Stock item updated.",
            data=StockItemDetailSerializer(_stock_detail(entity, item.pk)).data)


class StockIssueView(_ProcBase):
    """POST — issue stock out at moving-average cost (Dr expense, Cr inventory).

    docstring-name: Issue stock
    """

    rbac_permission = "procurement.stock.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        movement = stock.issue_stock(
            item,
            # quantity: strictly positive, finite, bounded (over-issue is caught in the service).
            quantity=_quantity(body.get("quantity"), "quantity"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            # An override expense account, if given, must be an active postable EXPENSE.
            expense_account=_resolve_expense_account(
                entity, body.get("expense_account"), "expense_account"),
            actor_user=request.user,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
        )
        return success_response(
            "Stock issued.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": StockItemDetailSerializer(_stock_detail(entity, item.pk)).data,
            },
            status=201,
        )


class StockAdjustView(_ProcBase):
    """POST — apply a signed stock-count correction (write-up or shrinkage).

    docstring-name: Adjust stock
    """

    rbac_permission = "procurement.stock.adjust"

    def post(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        unit_cost = body.get("unit_cost")
        movement = stock.adjust_stock(
            item,
            # A signed, non-zero, finite delta (+ write-up, − shrinkage); the service guards
            # a decrease against on-hand and picks the write-up/shrinkage accounts.
            quantity_delta=_signed_qty(body.get("quantity_delta"), "quantity_delta"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            # Adjustment account, if given, must be active postable EXPENSE (defaults to 5150).
            adjustment_account=_resolve_expense_account(
                entity, body.get("adjustment_account"), "adjustment_account"),
            # unit_cost only applies to an increase; strict integer kobo when provided.
            unit_cost=_strict_kobo(unit_cost, "unit_cost") if unit_cost not in (None, "") else None,
            actor_user=request.user,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
        )
        return success_response(
            "Stock adjusted.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": StockItemDetailSerializer(_stock_detail(entity, item.pk)).data,
            },
            status=201,
        )


class StockItemSummaryView(_ProcBase):
    """GET — entity-wide stock-item KPI strip (tracked / active / low / out / value).

    docstring-name: Stock items summary
    """

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        # ONE aggregate over the item rows — conditional counts avoid loading any rows.
        # low_stock: active, at/below its reorder level but still holding something;
        # out_of_stock: active with nothing on hand. total_value sums the carried kobo.
        agg = StockItem.objects.filter(entity=entity).aggregate(
            tracked=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            low_stock=Count("id", filter=Q(
                is_active=True, on_hand_qty__lte=F("reorder_level"), on_hand_qty__gt=0)),
            out_of_stock=Count("id", filter=Q(is_active=True, on_hand_qty__lte=0)),
            total_value=Sum("stock_value"),
        )
        # total_value is a flat kobo integer (+ a formatted naira string), matching the
        # ContractSummary KPI shape the FE strip consumes with formatMoney().
        total_value = agg["total_value"] or 0
        return success_response(
            "Stock summary retrieved.",
            data={
                "tracked": agg["tracked"] or 0,
                "active": agg["active"] or 0,
                "low_stock": agg["low_stock"] or 0,
                "out_of_stock": agg["out_of_stock"] or 0,
                "total_value": total_value,
                "total_value_naira": format_naira(total_value),
            },
        )


class StockMovementListView(_ProcBase):
    """GET — the stock ledger (movements), optionally filtered to one item.

    docstring-name: Stock movements
    """

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = StockMovement.objects.filter(entity=entity).select_related(
            "stock_item", "created_by")
        if (item_ref := request.query_params.get("stock_item")):
            qs = qs.filter(stock_item_id=item_ref) if str(item_ref).isdigit() \
                else qs.filter(stock_item__code=item_ref)
        if (mtype := request.query_params.get("movement_type")):
            qs = qs.filter(movement_type=mtype)
        return self.paginate(request, qs.order_by("-id"), StockMovementSerializer)


class StockReorderReportView(_ProcBase):
    """docstring-name: Stock reorder report"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        rows = stock.reorder_report(entity)
        return success_response(
            "Stock reorder report retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        "stock_item_id": r["stock_item_id"], "code": r["code"],
                        "name": r["name"], "on_hand_qty": str(r["on_hand_qty"]),
                        "reorder_level": str(r["reorder_level"]),
                        "reorder_qty": str(r["reorder_qty"]),
                        "unit_cost": _kobo(r["unit_cost"]),
                    }
                    for r in rows
                ],
            },
        )


class StockValuationReportView(_ProcBase):
    """docstring-name: Stock valuation report"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        report = stock.stock_valuation(entity)
        return success_response(
            "Stock valuation retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        "stock_item_id": r["stock_item_id"], "code": r["code"],
                        "name": r["name"], "on_hand_qty": str(r["on_hand_qty"]),
                        "unit_cost": _kobo(r["unit_cost"]),
                        "stock_value": _kobo(r["stock_value"]),
                    }
                    for r in report["rows"]
                ],
                "total_value": _kobo(report["total_value"]),
            },
        )
