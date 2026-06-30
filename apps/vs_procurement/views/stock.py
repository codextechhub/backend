"""Stock items and movements.
"""
from __future__ import annotations

import datetime

from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import stock
from ..models import (
    StockItem,
    StockMovement,
)
from ..serializers import (
    StockItemSerializer,
    StockMovementSerializer,
)


from .base import (
    _kobo,
    _ProcBase,
    _date,
    _dec,
    _money,
    _resolve_account,
)
from .catalog import _resolve_catalog_item

# --------------------------------------------------------------------------- #
# Inventory / stock ledger                                                     #
# --------------------------------------------------------------------------- #

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
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        if request.query_params.get("needs_reorder") == "true":
            from django.db.models import F
            qs = qs.filter(is_active=True, on_hand_qty__lte=F("reorder_level"))
        return self.paginate(request, qs.order_by("code"), StockItemSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        inventory = _resolve_account(
            entity, body.get("inventory_account"), "inventory_account")
        if inventory is None:
            raise ValidationError(
                {"inventory_account": "An inventory asset account is required."})
        item = StockItem.objects.create(
            entity=entity, code=body["code"], name=body["name"],
            description=body.get("description", ""),
            unit_of_measure=body.get("unit_of_measure") or "each",
            catalog_item=_resolve_catalog_item(entity, body.get("catalog_item")),
            inventory_account=inventory,
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            reorder_level=_dec(body.get("reorder_level", 0), "reorder_level"),
            reorder_qty=_dec(body.get("reorder_qty", 0), "reorder_qty"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Stock item created.", data=StockItemSerializer(item).data, status=201,
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
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        return success_response("Stock item retrieved.", data=StockItemSerializer(item).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        if "name" in body:
            item.name = body["name"]
        if "description" in body:
            item.description = body["description"]
        if "unit_of_measure" in body:
            item.unit_of_measure = body["unit_of_measure"] or "each"
        if "catalog_item" in body:
            item.catalog_item = _resolve_catalog_item(entity, body.get("catalog_item"))
        if "inventory_account" in body:
            inv = _resolve_account(entity, body.get("inventory_account"), "inventory_account")
            if inv is None:
                raise ValidationError(
                    {"inventory_account": "An inventory asset account is required."})
            item.inventory_account = inv
        if "default_expense_account" in body:
            item.default_expense_account = _resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account")
        if "reorder_level" in body:
            item.reorder_level = _dec(body.get("reorder_level", 0), "reorder_level")
        if "reorder_qty" in body:
            item.reorder_qty = _dec(body.get("reorder_qty", 0), "reorder_qty")
        if "is_active" in body:
            item.is_active = bool(body["is_active"])
        item.save()
        return success_response("Stock item updated.", data=StockItemSerializer(item).data)


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
            quantity=_dec(body.get("quantity"), "quantity"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            expense_account=_resolve_account(
                entity, body.get("expense_account"), "expense_account"),
            actor_user=request.user,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
        )
        item.refresh_from_db()
        return success_response(
            "Stock issued.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": StockItemSerializer(item).data,
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
            quantity_delta=_dec(body.get("quantity_delta"), "quantity_delta"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            adjustment_account=_resolve_account(
                entity, body.get("adjustment_account"), "adjustment_account"),
            unit_cost=_money(unit_cost, "unit_cost") if unit_cost not in (None, "") else None,
            actor_user=request.user,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
        )
        item.refresh_from_db()
        return success_response(
            "Stock adjusted.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": StockItemSerializer(item).data,
            },
            status=201,
        )


class StockMovementListView(_ProcBase):
    """GET — the stock ledger (movements), optionally filtered to one item.

    docstring-name: Stock movements
    """

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = StockMovement.objects.filter(entity=entity).select_related("stock_item")
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

