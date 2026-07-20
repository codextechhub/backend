"""Entity-scoped purchasing catalog master data and real usage insights."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, F, Max, Min, Q, Sum, Value, When
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.constants import AccountType, DocumentStatus
from vs_finance.views import resolve_entity
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission

from ..models import CatalogItem, PurchaseOrderLine, Vendor, VendorCategory
from ..purchasing import vendor_purchase_block_reason
from ..serializers import CatalogItemSerializer
from .base import _ProcBase, _resolve_account, _resolve_tax


def _has_permission(request, permission):
    if is_vision_super_admin(request.user):
        return True
    tenant = getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
    return user_has_rbac_permission(
        request.user, permission,
        tenant=tenant or getattr(request.user, "tenant", None),
        branch=getattr(request, "branch", None),
    )


def _clean_text(body, field, max_length, *, upper=False):
    value = str(body.get(field) or "").strip()
    value = value.upper() if upper else value
    if len(value) > max_length:
        raise ValidationError({field: f"Ensure this field has no more than {max_length} characters."})
    return value


def _strict_bool(value, field):
    if not isinstance(value, bool):
        raise ValidationError({field: "Enter a valid boolean value."})
    return value


def _strict_price(value):
    # JSON booleans and floats must not cross the integer-kobo boundary by coercion.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError({"standard_unit_price": "Expected a whole integer amount in kobo."})
    if value < 0:
        raise ValidationError({"standard_unit_price": "Amount cannot be negative."})
    if value > 9_223_372_036_854_775_807:
        raise ValidationError({"standard_unit_price": "Amount is too large."})
    return value


def _lead_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError({"lead_time_days": "Expected a whole number of days."})
    if value < 0 or value > 32767:
        raise ValidationError({"lead_time_days": "Lead time must be between 0 and 32767 days."})
    return value


def _resolve_category(entity, ref, *, current_id=None):
    if ref in (None, ""):
        return None
    qs = VendorCategory.objects.filter(entity=entity).select_related("parent", "parent__parent")
    category = qs.filter(pk=ref).first() if str(ref).isdigit() else qs.filter(code__iexact=str(ref)).first()
    if category is None:
        raise ValidationError({"category": "No such category in this entity."})
    if not category.is_active and category.pk != current_id:
        raise ValidationError({"category": "Select an active category."})
    return category


def _resolve_optional_vendor(entity, ref, field="preferred_vendor", *, current_id=None):
    if ref in (None, ""):
        return None
    qs = Vendor.objects.filter(entity=entity)
    vendor = qs.filter(pk=int(ref)).first() if str(ref).isdigit() else qs.filter(code__iexact=str(ref)).first()
    if vendor is None:
        raise ValidationError({field: f"No vendor '{ref}' in this entity."})
    reason = vendor_purchase_block_reason(vendor)
    if reason and vendor.pk != current_id:
        raise ValidationError({field: reason})
    return vendor


def _resolve_expense(entity, ref, *, current_id=None):
    account = _resolve_account(entity, ref, "default_expense_account")
    if account is None:
        return None
    valid = account.account_type == AccountType.EXPENSE and account.is_active and account.is_postable
    if not valid and account.pk != current_id:
        raise ValidationError({
            "default_expense_account": "Select an active, postable EXPENSE account in this entity.",
        })
    return account


def _resolve_purchase_tax(entity, ref, *, current_id=None):
    tax = _resolve_tax(entity, ref, "default_tax_code")
    if tax is None:
        return None
    paid = tax.paid_account
    valid_paid = paid is not None and paid.is_active and paid.is_postable and paid.account_type == AccountType.ASSET
    valid = tax.is_active and (tax.rate_bps == 0 or (tax.is_recoverable and valid_paid))
    if not valid and tax.pk != current_id:
        raise ValidationError({
            "default_tax_code": (
                "Select an active purchase tax code with a usable recoverable input-tax account."
            ),
        })
    return tax


def _resolve_catalog_item(entity, ref, field="catalog_item", *, current_id=None):
    """Resolve an assignable item while retaining an existing inactive stock link."""
    if ref in (None, ""):
        return None
    qs = CatalogItem.objects.filter(entity=entity).select_related(
        "category__default_expense_account", "default_expense_account",
        "default_tax_code__paid_account",
    )
    item = qs.filter(pk=int(ref)).first() if str(ref).isdigit() else qs.filter(code__iexact=str(ref)).first()
    if item is None:
        raise ValidationError({field: f"No catalog item '{ref}' in this entity."})
    if not item.is_active and item.pk != current_id:
        raise ValidationError({field: "Select an active catalog item."})
    return item


def _catalog_queryset(entity, *, include_stock):
    qs = CatalogItem.objects.filter(entity=entity).select_related(
        "category", "category__parent", "category__parent__parent",
        "preferred_vendor", "default_expense_account", "default_tax_code",
    )
    if not include_stock:
        return qs.annotate(stock_status=Value(None, output_field=CharField()))
    qs = qs.annotate(
        # Every stock join repeats entity scope so legacy cross-entity ORM rows cannot leak.
        active_stock_count=Count(
            "stock_items",
            filter=Q(stock_items__entity=entity, stock_items__is_active=True),
            distinct=True,
        ),
        empty_stock_count=Count(
            "stock_items",
            filter=Q(
                stock_items__entity=entity, stock_items__is_active=True,
                stock_items__on_hand_qty__lte=0,
            ),
            distinct=True,
        ),
        low_stock_count=Count(
            "stock_items",
            filter=Q(
                stock_items__entity=entity, stock_items__is_active=True,
                stock_items__on_hand_qty__gt=0,
                stock_items__on_hand_qty__lte=F("stock_items__reorder_level"),
            ),
            distinct=True,
        ),
    )
    return qs.annotate(stock_status=Case(
        When(active_stock_count=0, then=Value("NOT_TRACKED")),
        When(empty_stock_count=F("active_stock_count"), then=Value("OUT_OF_STOCK")),
        When(Q(empty_stock_count__gt=0) | Q(low_stock_count__gt=0), then=Value("LOW_STOCK")),
        default=Value("IN_STOCK"), output_field=CharField(),
    ))


def _duplicate_error(exc):
    return ValidationError({"code": "A catalog item with this code already exists in this entity."})


class CatalogItemListCreateView(_ProcBase):
    """List or create reusable buying defaults; no operation here posts to the GL."""

    @property
    def rbac_permission(self):
        return "procurement.catalog_item.create" if self.request.method == "POST" \
            else "procurement.catalog_item.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _catalog_queryset(
            entity, include_stock=_has_permission(request, "procurement.stock.view"),
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(preferred_vendor_id=vendor) if str(vendor).isdigit() \
                else qs.filter(preferred_vendor__code__iexact=vendor)
        if (category := request.query_params.get("category")):
            qs = qs.filter(category_id=category) if str(category).isdigit() \
                else qs.filter(category__code__iexact=category)
        if (search := (request.query_params.get("search") or request.query_params.get("q") or "").strip()):
            qs = qs.filter(
                Q(code__icontains=search) | Q(name__icontains=search)
                | Q(description__icontains=search) | Q(category__name__icontains=search)
                | Q(preferred_vendor__name__icontains=search)
            )
        return self.paginate(request, qs.order_by("code", "id"), CatalogItemSerializer)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        code = _clean_text(body, "code", 40, upper=True)
        name = _clean_text(body, "name", 200)
        if not code or not name:
            raise ValidationError({"code": "Code and name are required."})
        description = _clean_text(body, "description", 255)
        unit = _clean_text({"unit_of_measure": body.get("unit_of_measure", "each")}, "unit_of_measure", 24)
        if not unit:
            raise ValidationError({"unit_of_measure": "Unit of measure is required."})
        try:
            # The CI database constraint is the final guard against concurrent duplicates.
            item = CatalogItem.objects.create(
                entity=entity, code=code, name=name, description=description,
                unit_of_measure=unit,
                category=_resolve_category(entity, body.get("category")),
                preferred_vendor=_resolve_optional_vendor(entity, body.get("preferred_vendor")),
                default_expense_account=_resolve_expense(entity, body.get("default_expense_account")),
                default_tax_code=_resolve_purchase_tax(entity, body.get("default_tax_code")),
                lead_time_days=_lead_time(body.get("lead_time_days")),
                standard_unit_price=_strict_price(body.get("standard_unit_price", 0)),
                is_active=_strict_bool(body.get("is_active", True), "is_active"),
            )
        except IntegrityError as exc:
            raise _duplicate_error(exc) from exc
        return success_response(
            "Catalog item created.", data=CatalogItemSerializer(item).data, status=201,
        )


class CatalogItemDetailView(_ProcBase):
    """Retrieve or update one item without rewriting any snapshotted document line."""

    @property
    def rbac_permission(self):
        return "procurement.catalog_item.update" if self.request.method == "PATCH" \
            else "procurement.catalog_item.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        item = _catalog_queryset(
            entity, include_stock=_has_permission(request, "procurement.stock.view"),
        ).filter(pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")
        return success_response("Catalog item retrieved.", data=CatalogItemSerializer(item).data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        item = CatalogItem.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")
        body = request.data
        if "code" in body and _clean_text(body, "code", 40, upper=True) != item.code:
            raise ValidationError({"code": "Catalog item code cannot be changed after creation."})
        if "name" in body:
            item.name = _clean_text(body, "name", 200)
            if not item.name:
                raise ValidationError({"name": "Name is required."})
        if "description" in body:
            item.description = _clean_text(body, "description", 255)
        if "unit_of_measure" in body:
            item.unit_of_measure = _clean_text(body, "unit_of_measure", 24)
            if not item.unit_of_measure:
                raise ValidationError({"unit_of_measure": "Unit of measure is required."})
        if "category" in body:
            item.category = _resolve_category(entity, body.get("category"), current_id=item.category_id)
        if "preferred_vendor" in body:
            item.preferred_vendor = _resolve_optional_vendor(
                entity, body.get("preferred_vendor"), current_id=item.preferred_vendor_id,
            )
        if "default_expense_account" in body:
            item.default_expense_account = _resolve_expense(
                entity, body.get("default_expense_account"), current_id=item.default_expense_account_id,
            )
        if "default_tax_code" in body:
            item.default_tax_code = _resolve_purchase_tax(
                entity, body.get("default_tax_code"), current_id=item.default_tax_code_id,
            )
        if "lead_time_days" in body:
            item.lead_time_days = _lead_time(body.get("lead_time_days"))
        if "standard_unit_price" in body:
            item.standard_unit_price = _strict_price(body.get("standard_unit_price"))
        if "is_active" in body:
            item.is_active = _strict_bool(body.get("is_active"), "is_active")
        item.save()
        item = _catalog_queryset(entity, include_stock=False).get(pk=item.pk)
        return success_response("Catalog item updated.", data=CatalogItemSerializer(item).data)


class CatalogItemInsightsView(_ProcBase):
    """Report-gated, authoritative purchasing history for one catalog item."""

    rbac_permission = "procurement.report.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        item = CatalogItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")

        # Only issued commitments count as history; draft/pending orders are not realised vendor pricing.
        lines = PurchaseOrderLine.objects.filter(
            requisition_line__catalog_item=item,
            requisition_line__requisition__entity=entity,
            purchase_order__entity=entity,
            purchase_order__vendor__entity=entity,
            purchase_order__status=DocumentStatus.APPROVED,
        )
        vendor_pricing = lines.values(
            "purchase_order__vendor_id", "purchase_order__vendor__code",
            "purchase_order__vendor__name",
        ).annotate(
            order_count=Count("purchase_order_id", distinct=True),
            total_quantity=Sum("quantity"),
            minimum_unit_price=Min("unit_price"),
            maximum_unit_price=Max("unit_price"),
            latest_order_date=Max("purchase_order__order_date"),
        ).order_by("purchase_order__vendor__code")
        history = lines.select_related("purchase_order", "purchase_order__vendor").order_by(
            "-purchase_order__order_date", "-id",
        )[:10]
        return success_response("Catalog item insights retrieved.", data={
            "usage": {
                # Counts are scoped through both sides of every historical relationship.
                "requisition_line_count": item.requisition_lines.filter(
                    requisition__entity=entity,
                ).count(),
                "stock_item_count": item.stock_items.filter(entity=entity).count(),
            },
            "vendor_pricing": [{
                "vendor_id": row["purchase_order__vendor_id"],
                "vendor_code": row["purchase_order__vendor__code"],
                "vendor_name": row["purchase_order__vendor__name"],
                "order_count": row["order_count"],
                "total_quantity": str(row["total_quantity"] or 0),
                "minimum_unit_price": row["minimum_unit_price"] or 0,
                "maximum_unit_price": row["maximum_unit_price"] or 0,
                "latest_order_date": row["latest_order_date"],
            } for row in vendor_pricing],
            "purchase_history": [{
                "purchase_order_id": line.purchase_order_id,
                "document_number": line.purchase_order.document_number,
                "vendor_code": line.purchase_order.vendor.code,
                "vendor_name": line.purchase_order.vendor.name,
                "order_date": line.purchase_order.order_date,
                "quantity": str(line.quantity),
                "unit_price": line.unit_price,
            } for line in history],
        })
