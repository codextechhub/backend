"""Vendor categories and vendor master data.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from ..models import (
    Vendor,
    VendorCategory,
)
from ..serializers import (
    VendorCategorySerializer,
    VendorSerializer,
)


from .base import (
    _ProcBase,
    _resolve_account,
    _resolve_tax,
)

# --------------------------------------------------------------------------- #
# Vendor categories + vendors                                                 #
# --------------------------------------------------------------------------- #

class VendorCategoryListCreateView(_ProcBase):
    """GET (list) / POST (create) vendor categories for an entity.

    docstring-name: Vendor categories
    """

    @property
    def rbac_permission(self):
        return "procurement.category.create" if self.request.method == "POST" \
            else "procurement.category.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorCategory.objects.filter(entity=entity).select_related("default_expense_account")
        return success_response(
            "Vendor categories retrieved.",
            data=VendorCategorySerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        cat = VendorCategory.objects.create(
            entity=entity, code=body["code"], name=body["name"],
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Vendor category created.", data=VendorCategorySerializer(cat).data, status=201,
        )


class VendorListCreateView(_ProcBase):
    """GET (list) / POST (create) vendors for an entity.

    docstring-name: Vendors
    """

    @property
    def rbac_permission(self):
        return "procurement.vendor.create" if self.request.method == "POST" \
            else "procurement.vendor.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Vendor.objects.filter(entity=entity).select_related("category", "payable_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (hold := request.query_params.get("on_hold")) in ("true", "false"):
            qs = qs.filter(on_hold=hold == "true")
        return success_response(
            "Vendors retrieved.", data=VendorSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        category = None
        if body.get("category"):
            category = VendorCategory.objects.filter(entity=entity, pk=body["category"]).first() \
                or VendorCategory.objects.filter(entity=entity, code=body["category"]).first()
            if category is None:
                raise ValidationError({"category": "No such vendor category in this entity."})
        vendor = Vendor.objects.create(
            entity=entity, code=body["code"], name=body["name"], category=category,
            email=body.get("email", ""), phone=body.get("phone", ""),
            tax_id=body.get("tax_id", ""),
            bank_name=body.get("bank_name", ""),
            bank_account_number=body.get("bank_account_number", ""),
            bank_account_name=body.get("bank_account_name", ""),
            payable_account=_resolve_account(entity, body.get("payable_account"), "payable_account"),
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            default_wht_tax_code=_resolve_tax(entity, body.get("default_wht_tax_code"),
                                              "default_wht_tax_code"),
            payment_terms=body.get("payment_terms") or "NET_30",
            kyc_status=body.get("kyc_status") or "PENDING",
            risk=body.get("risk") or "LOW",
            on_hold=bool(body.get("on_hold", False)),
        )
        return success_response(
            "Vendor created.", data=VendorSerializer(vendor).data, status=201,
        )


class VendorDetailView(_ProcBase):
    """docstring-name: Vendors"""
    rbac_permission = "procurement.vendor.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        vendor = Vendor.objects.filter(entity=entity, pk=pk).first()
        if vendor is None:
            raise NotFound("No such vendor in this entity.")
        return success_response("Vendor retrieved.", data=VendorSerializer(vendor).data)


