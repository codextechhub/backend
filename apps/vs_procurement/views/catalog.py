"""Catalog items.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from ..models import (
    CatalogItem,
    Vendor,
)
from ..serializers import (
    CatalogItemSerializer,
)


from .base import (
    _ProcBase,
    _money,
    _resolve_account,
    _resolve_tax,
)

# --------------------------------------------------------------------------- #
# Item catalog                                                                #
# --------------------------------------------------------------------------- #

def _resolve_catalog_item(entity, ref, field="catalog_item"):
    """Resolve a catalog item by id/code, or ``None`` when ``ref`` is blank."""
    if ref in (None, ""):
        return None
    qs = CatalogItem.objects.filter(entity=entity)
    item = qs.filter(pk=int(ref)).first() if str(ref).isdigit() else qs.filter(code=str(ref)).first()
    if item is None:
        raise ValidationError({field: f"No catalog item '{ref}' in this entity."})
    return item


def _resolve_optional_vendor(entity, ref, field="preferred_vendor"):
    """Resolve a vendor by id/code, or ``None`` when ``ref`` is blank."""
    if ref in (None, ""):
        return None
    qs = Vendor.objects.filter(entity=entity)
    vendor = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref)).first() or qs.filter(code=str(ref).upper()).first()
    )
    if vendor is None:
        raise ValidationError({field: f"No vendor '{ref}' in this entity."})
    return vendor


class CatalogItemListCreateView(_ProcBase):
    """GET (list) / POST (create) catalog items — reusable buying defaults.

    docstring-name: Catalog items
    """

    @property
    def rbac_permission(self):
        return "procurement.catalog_item.create" if self.request.method == "POST" \
            else "procurement.catalog_item.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = CatalogItem.objects.filter(entity=entity).select_related(
            "preferred_vendor", "default_expense_account", "default_tax_code")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(preferred_vendor_id=vendor) if str(vendor).isdigit() \
                else qs.filter(preferred_vendor__code=vendor)
        if (search := request.query_params.get("q")):
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return success_response(
            "Catalog items retrieved.",
            data=CatalogItemSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        item = CatalogItem.objects.create(
            entity=entity, code=body["code"], name=body["name"],
            description=body.get("description", ""),
            unit_of_measure=body.get("unit_of_measure") or "each",
            preferred_vendor=_resolve_optional_vendor(entity, body.get("preferred_vendor")),
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            default_tax_code=_resolve_tax(entity, body.get("default_tax_code"), "default_tax_code"),
            lead_time_days=body.get("lead_time_days") or None,
            standard_unit_price=_money(body.get("standard_unit_price", 0), "standard_unit_price"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Catalog item created.", data=CatalogItemSerializer(item).data, status=201,
        )


class CatalogItemDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update buying defaults) one catalog item.

    docstring-name: Catalog items
    """

    @property
    def rbac_permission(self):
        return "procurement.catalog_item.update" if self.request.method == "PATCH" \
            else "procurement.catalog_item.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        item = CatalogItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")
        return success_response("Catalog item retrieved.", data=CatalogItemSerializer(item).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        item = CatalogItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")
        body = request.data
        if "name" in body:
            item.name = body["name"]
        if "description" in body:
            item.description = body["description"]
        if "unit_of_measure" in body:
            item.unit_of_measure = body["unit_of_measure"] or "each"
        if "preferred_vendor" in body:
            item.preferred_vendor = _resolve_optional_vendor(entity, body.get("preferred_vendor"))
        if "default_expense_account" in body:
            item.default_expense_account = _resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account")
        if "default_tax_code" in body:
            item.default_tax_code = _resolve_tax(entity, body.get("default_tax_code"), "default_tax_code")
        if "lead_time_days" in body:
            item.lead_time_days = body.get("lead_time_days") or None
        if "standard_unit_price" in body:
            item.standard_unit_price = _money(body.get("standard_unit_price", 0), "standard_unit_price")
        if "is_active" in body:
            item.is_active = bool(body["is_active"])
        item.save()
        return success_response("Catalog item updated.", data=CatalogItemSerializer(item).data)


