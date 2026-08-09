"""Typed Procurement settings resolution and audited updates."""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction

from .constants import PaymentTerms, VendorPurchaseKycRequirement
from .models import ProcurementSettings


SETTING_FIELDS = (
    "default_payment_terms",
    "default_delivery_address",
    "quantity_tolerance_bps",
    "price_tolerance_bps",
    "allow_non_po_invoices",
    "vendor_purchase_kyc_requirement",
    "require_purchase_order_for_receipts",
    "default_requisition_lead_days",
    "contract_renewal_notice_days",
    "default_rfq_response_days",
    "rfq_closing_soon_days",
)


def resolve_procurement_settings(entity):
    """Return persisted settings or an unsaved object carrying model defaults."""
    settings = ProcurementSettings.objects.filter(entity=entity).first()
    return settings or ProcurementSettings(entity=entity)


def serialize_procurement_settings(settings):
    return {
        "default_payment_terms": settings.default_payment_terms,
        "default_payment_terms_label": PaymentTerms(settings.default_payment_terms).label,
        "default_delivery_address": settings.default_delivery_address,
        "quantity_tolerance_bps": settings.quantity_tolerance_bps,
        "price_tolerance_bps": settings.price_tolerance_bps,
        "allow_non_po_invoices": settings.allow_non_po_invoices,
        "vendor_purchase_kyc_requirement": settings.vendor_purchase_kyc_requirement,
        "vendor_purchase_kyc_requirement_label": VendorPurchaseKycRequirement(
            settings.vendor_purchase_kyc_requirement,
        ).label,
        "require_purchase_order_for_receipts": settings.require_purchase_order_for_receipts,
        "default_requisition_lead_days": settings.default_requisition_lead_days,
        "contract_renewal_notice_days": settings.contract_renewal_notice_days,
        "default_rfq_response_days": settings.default_rfq_response_days,
        "rfq_closing_soon_days": settings.rfq_closing_soon_days,
        "updated_at": settings.updated_at.isoformat() if settings.pk else None,
        "updated_by": settings.updated_by.email if settings.pk and settings.updated_by else None,
    }


def _validated_values(data):
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one setting."})
    unknown = sorted(set(data) - set(SETTING_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    values = {}
    if "default_payment_terms" in data:
        value = str(data["default_payment_terms"] or "")
        if value not in PaymentTerms.values:
            raise ValidationError({"default_payment_terms": "Select a valid payment term."})
        values["default_payment_terms"] = value
    if "default_delivery_address" in data:
        value = str(data["default_delivery_address"] or "").strip()
        if len(value) > 2000:
            raise ValidationError({"default_delivery_address": "Use 2,000 characters or fewer."})
        values["default_delivery_address"] = value
    for field in ("quantity_tolerance_bps", "price_tolerance_bps"):
        if field not in data:
            continue
        try:
            value = int(data[field])
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Enter a whole number of basis points."}) from exc
        if value < 0 or value > 10000:
            raise ValidationError({field: "Use a value from 0 to 10,000 basis points."})
        values[field] = value
    for field in ("allow_non_po_invoices", "require_purchase_order_for_receipts"):
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, bool):
            raise ValidationError({field: "Use true or false."})
        values[field] = value
    if "vendor_purchase_kyc_requirement" in data:
        value = str(data["vendor_purchase_kyc_requirement"] or "")
        if value not in VendorPurchaseKycRequirement.values:
            raise ValidationError({
                "vendor_purchase_kyc_requirement": "Select a valid KYC requirement.",
            })
        values["vendor_purchase_kyc_requirement"] = value
    for field in (
        "default_requisition_lead_days",
        "contract_renewal_notice_days",
        "default_rfq_response_days",
        "rfq_closing_soon_days",
    ):
        if field not in data:
            continue
        value = data[field]
        if isinstance(value, bool):
            raise ValidationError({field: "Enter a whole number of days."})
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Enter a whole number of days."}) from exc
        if value < 0 or value > 365:
            raise ValidationError({field: "Use a value from 0 to 365 days."})
        values[field] = value
    return values


@transaction.atomic
def update_procurement_settings(*, entity, data, actor_user):
    """Update one entity's settings and write the immutable audit row atomically."""
    values = _validated_values(data)
    current = ProcurementSettings.objects.select_for_update().filter(entity=entity).first()
    settings = current or ProcurementSettings(entity=entity)
    before_all = serialize_procurement_settings(settings)
    for field, value in values.items():
        setattr(settings, field, value)
    settings.updated_by = actor_user
    settings.full_clean()
    settings.save()
    after_all = serialize_procurement_settings(settings)
    changed_before = {
        field: before_all[field] for field in values
        if before_all[field] != after_all[field]
    }
    changed_after = {
        field: after_all[field] for field in values
        if before_all[field] != after_all[field]
    }
    if changed_after:
        record(
            entity=entity,
            action=FinanceAuditAction.PROCUREMENT_SETTINGS_UPDATED,
            actor_user=actor_user,
            target=settings,
            target_type="ProcurementSettings",
            message=f"Updated {len(changed_after)} procurement setting(s).",
            before=changed_before,
            after=changed_after,
        )
    return settings
