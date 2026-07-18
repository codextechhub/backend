"""Vendor categories and vendor master data.
"""
from __future__ import annotations


import re

from django.db import IntegrityError, transaction
from django.core.validators import validate_email
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity
from vs_finance.constants import AccountType, DocumentStatus
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission

from ..constants import PAYMENT_TERM_DAYS, PaymentTerms, VendorKycStatus, VendorRisk
from ..models import (
    Vendor,
    VendorCategory,
    VendorInvoice,
)
from ..serializers import (
    VendorCategorySerializer,
    VendorListSerializer,
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

_SENSITIVE_VENDOR_FIELDS = {
    "email", "phone", "address", "tax_id",
    "bank_name", "bank_account_number", "bank_account_name",
}


def _normalise_code(value):
    return str(value or "").strip().upper()


def _normalise_tax_id(value):
    # Tax identifiers compare without punctuation/case so formatting cannot bypass uniqueness.
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _clean_text(body, field, max_length, *, upper=False, lower=False):
    value = str(body.get(field) or "").strip()
    value = value.upper() if upper else value.lower() if lower else value
    if len(value) > max_length:
        raise ValidationError({field: f"Ensure this field has no more than {max_length} characters."})
    return value


def _validate_email(value):
    if value:
        try:
            validate_email(value)
        except Exception as exc:
            raise ValidationError({"email": "Enter a valid email address."}) from exc
    return value


def _has_sensitive_access(request):
    if is_vision_super_admin(request.user):
        return True
    tenant = getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
    return user_has_rbac_permission(
        request.user, "procurement.vendor.view_sensitive",
        tenant=tenant or getattr(request.user, "tenant", None), branch=getattr(request, "branch", None),
    )


def _require_sensitive_access(request, body):
    if _SENSITIVE_VENDOR_FIELDS.intersection(body) and not _has_sensitive_access(request):
        raise PermissionDenied("You do not have permission to modify sensitive vendor fields.")


def _resolve_category(entity, ref):
    if ref in (None, ""):
        return None
    qs = VendorCategory.objects.filter(entity=entity)
    category = qs.filter(pk=ref).first() if str(ref).isdigit() else qs.filter(code__iexact=str(ref)).first()
    if category is None:
        raise ValidationError({"category": "No such vendor category in this entity."})
    return category


def _validate_account_type(account, field, allowed):
    if account is not None and account.account_type not in allowed:
        labels = ", ".join(sorted(allowed))
        raise ValidationError({field: f"Select an active {labels} account in this entity."})
    if account is not None and (not account.is_active or not account.is_postable):
        raise ValidationError({field: "Select an active, postable account."})
    return account


def _validate_choice(value, choices, field):
    if value not in choices:
        raise ValidationError({field: "Select a valid value."})
    return value


def _validate_bool(value, field):
    if not isinstance(value, bool):
        raise ValidationError({field: "Enter a valid boolean value."})
    return value


def _duplicate_error(exc):
    text = str(exc)
    if "tax_id" in text:
        return ValidationError({"tax_id": "A vendor with this tax identifier already exists in this entity."})
    return ValidationError({"code": "A vendor with this code already exists in this entity."})

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
        return self.paginate(request, qs.order_by("code"), VendorCategorySerializer)

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
        qs = Vendor.objects.filter(entity=entity).select_related("category").annotate(
            # Only issued POs with at least one unreceived line remain open commitments.
            active_po_count=Count(
                "purchase_orders",
                filter=Q(
                    purchase_orders__status=DocumentStatus.APPROVED,
                    purchase_orders__lines__received_qty__lt=F("purchase_orders__lines__quantity"),
                ),
                distinct=True,
            ),
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (hold := request.query_params.get("on_hold")) in ("true", "false"):
            qs = qs.filter(on_hold=hold == "true")
        if (kyc := request.query_params.get("kyc_status")):
            qs = qs.filter(kyc_status=kyc)
        if request.query_params.get("purchase_eligible") == "true":
            # Pending KYC may be sourced, but rejected, inactive, and held vendors cannot receive commitments.
            qs = qs.filter(is_active=True, on_hold=False).exclude(kyc_status=VendorKycStatus.REJECTED)
        if (search := (request.query_params.get("search") or request.query_params.get("q") or "").strip()):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search) | Q(category__name__icontains=search))
        return self.paginate(request, qs.order_by("code"), VendorListSerializer)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        code = _clean_text(body, "code", 32, upper=True)
        name = _clean_text(body, "name", 200)
        if not code or not name:
            raise ValidationError({"code": "code and name are required."})
        _require_sensitive_access(request, body)
        tax_id = _clean_text(body, "tax_id", 32, upper=True)
        payable = _validate_account_type(
            _resolve_account(entity, body.get("payable_account"), "payable_account"),
            "payable_account", {AccountType.LIABILITY},
        )
        expense = _validate_account_type(
            _resolve_account(entity, body.get("default_expense_account"), "default_expense_account"),
            "default_expense_account", {AccountType.EXPENSE},
        )
        payment_terms = _validate_choice(body.get("payment_terms") or PaymentTerms.NET_30, PaymentTerms.values, "payment_terms")
        try:
            vendor = Vendor.objects.create(
                entity=entity, code=code, name=name, category=_resolve_category(entity, body.get("category")),
                email=_validate_email(_clean_text(body, "email", 254, lower=True)),
                phone=_clean_text(body, "phone", 32), address=str(body.get("address") or "").strip(), tax_id=tax_id,
                tax_id_normalized=_normalise_tax_id(tax_id),
                bank_name=_clean_text(body, "bank_name", 120),
                bank_account_number=re.sub(r"\s+", "", _clean_text(body, "bank_account_number", 32, upper=True)),
                bank_account_name=_clean_text(body, "bank_account_name", 160),
                payable_account=payable, default_expense_account=expense,
                default_wht_tax_code=_resolve_tax(entity, body.get("default_wht_tax_code"), "default_wht_tax_code"),
                payment_terms=payment_terms,
                # Onboarding never self-approves compliance or purchasing blocks.
                kyc_status=VendorKycStatus.PENDING, risk=VendorRisk.LOW, on_hold=False, is_active=True,
            )
        except IntegrityError as exc:
            raise _duplicate_error(exc) from exc
        return success_response(
            "Vendor created.", data=VendorSerializer(vendor, context={"request": request}).data, status=201,
        )


class VendorSummaryView(_ProcBase):
    """Entity-wide vendor KPIs; spend remains behind the report permission."""

    rbac_permission = "procurement.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        vendors = Vendor.objects.filter(entity=entity)
        year_start = timezone.localdate().replace(month=1, day=1)
        spend = VendorInvoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, invoice_date__gte=year_start,
        ).aggregate(total=Sum("total"))["total"] or 0
        terms = [PAYMENT_TERM_DAYS.get(value, 0) for value in vendors.values_list("payment_terms", flat=True)]
        return success_response("Vendor summary retrieved.", data={
            "active": vendors.filter(is_active=True, on_hold=False).count(),
            "inactive": vendors.filter(is_active=False).count(),
            "on_hold": vendors.filter(on_hold=True).count(),
            "kyc_pending": vendors.filter(kyc_status=VendorKycStatus.PENDING).count(),
            "total_spend_ytd": spend,
            "average_payment_days": round(sum(terms) / len(terms)) if terms else None,
        })


class VendorDetailView(_ProcBase):
    """docstring-name: Vendors"""

    @property
    def rbac_permission(self):
        return "procurement.vendor.update" if self.request.method == "PATCH" else "procurement.vendor.view"

    def _get(self, entity, pk, *, lock=False):
        if lock:
            # Lock only the vendor row; nullable account/category joins cannot be locked by PostgreSQL.
            vendor = Vendor.objects.select_for_update(of=("self",)).filter(entity=entity, pk=pk).first()
            if vendor is None:
                raise NotFound("No such vendor in this entity.")
            return vendor
        qs = Vendor.objects.select_related(
            "category", "payable_account", "default_expense_account", "default_wht_tax_code",
        ).filter(entity=entity, pk=pk)
        vendor = qs.first()
        if vendor is None:
            raise NotFound("No such vendor in this entity.")
        return vendor

    def get(self, request, pk):
        entity = resolve_entity(request)
        vendor = self._get(entity, pk)
        return success_response(
            "Vendor retrieved.", data=VendorSerializer(vendor, context={"request": request}).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        body = request.data
        _require_sensitive_access(request, body)
        vendor = self._get(entity, pk, lock=True)

        if "code" in body and _normalise_code(body.get("code")) != vendor.code:
            raise ValidationError({"code": "Vendor code cannot be changed after creation."})
        if "name" in body:
            vendor.name = _clean_text(body, "name", 200)
            if not vendor.name:
                raise ValidationError({"name": "Vendor name is required."})
        if "category" in body:
            vendor.category = _resolve_category(entity, body.get("category"))
        text_fields = {"email": 254, "phone": 32, "bank_name": 120, "bank_account_name": 160}
        for field, max_length in text_fields.items():
            if field in body:
                value = _clean_text(body, field, max_length, lower=field == "email")
                setattr(vendor, field, _validate_email(value) if field == "email" else value)
        if "address" in body:
            vendor.address = str(body.get("address") or "").strip()
        if "bank_account_number" in body:
            vendor.bank_account_number = re.sub(r"\s+", "", _clean_text(body, "bank_account_number", 32, upper=True))
        if "tax_id" in body:
            vendor.tax_id = _clean_text(body, "tax_id", 32, upper=True)
            vendor.tax_id_normalized = _normalise_tax_id(vendor.tax_id)
        if "payable_account" in body:
            vendor.payable_account = _validate_account_type(
                _resolve_account(entity, body.get("payable_account"), "payable_account"),
                "payable_account", {AccountType.LIABILITY},
            )
        if "default_expense_account" in body:
            vendor.default_expense_account = _validate_account_type(
                _resolve_account(entity, body.get("default_expense_account"), "default_expense_account"),
                "default_expense_account", {AccountType.EXPENSE},
            )
        if "default_wht_tax_code" in body:
            vendor.default_wht_tax_code = _resolve_tax(entity, body.get("default_wht_tax_code"), "default_wht_tax_code")
        if "payment_terms" in body:
            vendor.payment_terms = _validate_choice(body.get("payment_terms"), PaymentTerms.values, "payment_terms")
        if "kyc_status" in body:
            vendor.kyc_status = _validate_choice(body.get("kyc_status"), VendorKycStatus.values, "kyc_status")
        if "risk" in body:
            vendor.risk = _validate_choice(body.get("risk"), VendorRisk.values, "risk")
        if "on_hold" in body:
            vendor.on_hold = _validate_bool(body.get("on_hold"), "on_hold")
        if "is_active" in body:
            vendor.is_active = _validate_bool(body.get("is_active"), "is_active")
        try:
            vendor.save()
        except IntegrityError as exc:
            raise _duplicate_error(exc) from exc
        return success_response(
            "Vendor updated.", data=VendorSerializer(vendor, context={"request": request}).data,
        )


class VendorInsightsView(_ProcBase):
    """Authoritative spend and operational performance for one entity-scoped vendor."""

    rbac_permission = "procurement.report.view"

    def get(self, request, pk):
        from ..reports import spend_analysis, vendor_performance

        entity = resolve_entity(request)
        vendor = Vendor.objects.filter(entity=entity, pk=pk).first()
        if vendor is None:
            raise NotFound("No such vendor in this entity.")
        year_start = timezone.localdate().replace(month=1, day=1)
        # Scope both reports to this vendor so the drawer doesn't recompute the whole entity.
        spend_row = next((row for row in spend_analysis(entity, start_date=year_start, vendor=vendor).by_vendor if row.key == vendor.code), None)
        perf_row = next((row for row in vendor_performance(entity, start_date=year_start, vendor=vendor).rows if row.vendor_id == vendor.id), None)
        return success_response("Vendor insights retrieved.", data={
            "spend_ytd": spend_row.gross if spend_row else 0,
            "invoice_count": spend_row.invoice_count if spend_row else 0,
            "po_count": perf_row.po_count if perf_row else 0,
            "total_ordered": perf_row.total_ordered if perf_row else 0,
            "receipt_count": perf_row.receipt_count if perf_row else 0,
            "on_time_receipts": perf_row.on_time_receipts if perf_row else 0,
            "late_receipts": perf_row.late_receipts if perf_row else 0,
            "on_time_rate": perf_row.on_time_rate if perf_row else None,
            "payment_count": perf_row.payment_count if perf_row else 0,
            "total_paid": perf_row.total_paid if perf_row else 0,
            "average_payment_days": perf_row.avg_payment_days if perf_row else None,
        })
