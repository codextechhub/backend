"""Typed bank-reconciliation and receipt-allocation defaults."""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import FinanceAuditAction
from .models import FinanceBankingSettings


SETTING_FIELDS = (
    "default_bank_reconciliation_tolerance_days",
    "default_group_reconciliation_matches",
    "default_receipt_allocation_strategy",
    "petty_cash_low_balance_threshold_bps",
)


def resolve_finance_banking_settings(entity):
    """Return stored settings or an unsaved object carrying safe defaults."""
    settings = FinanceBankingSettings.objects.filter(entity=entity).first()
    return settings or FinanceBankingSettings(entity=entity)


def serialize_finance_banking_settings(settings):
    return {
        "default_bank_reconciliation_tolerance_days": (
            settings.default_bank_reconciliation_tolerance_days
        ),
        "default_group_reconciliation_matches": (
            settings.default_group_reconciliation_matches
        ),
        "default_receipt_allocation_strategy": (
            settings.default_receipt_allocation_strategy
        ),
        "default_receipt_allocation_strategy_label": (
            FinanceBankingSettings.ReceiptAllocationStrategy(
                settings.default_receipt_allocation_strategy,
            ).label
        ),
        "petty_cash_low_balance_threshold_bps": (
            settings.petty_cash_low_balance_threshold_bps
        ),
        "updated_at": settings.updated_at.isoformat() if settings.pk else None,
        "updated_by": settings.updated_by.email if settings.pk and settings.updated_by else None,
    }


def _validated_values(data):
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one banking setting."})
    unknown = sorted(set(data) - set(SETTING_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    values = {}
    field = "default_bank_reconciliation_tolerance_days"
    if field in data:
        value = data[field]
        if isinstance(value, bool):
            raise ValidationError({field: "Enter a whole number of days."})
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Enter a whole number of days."}) from exc
        if value < 0 or value > 30:
            raise ValidationError({field: "Use a value from 0 to 30 days."})
        values[field] = value

    field = "default_group_reconciliation_matches"
    if field in data:
        if not isinstance(data[field], bool):
            raise ValidationError({field: "Use true or false."})
        values[field] = data[field]

    field = "default_receipt_allocation_strategy"
    if field in data:
        value = str(data[field] or "").lower()
        if value not in FinanceBankingSettings.ReceiptAllocationStrategy.values:
            raise ValidationError({field: "Select a supported allocation strategy."})
        values[field] = value

    field = "petty_cash_low_balance_threshold_bps"
    if field in data:
        value = data[field]
        if isinstance(value, bool):
            raise ValidationError({field: "Enter a whole number of basis points."})
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Enter a whole number of basis points."}) from exc
        if value < 0 or value > 10000:
            raise ValidationError({field: "Use a value from 0 to 10,000 basis points."})
        values[field] = value
    return values


@transaction.atomic
def update_finance_banking_settings(*, entity, data, actor_user):
    """Apply a partial banking-policy update and audit effective changes."""
    values = _validated_values(data)
    current = FinanceBankingSettings.objects.select_for_update().filter(entity=entity).first()
    settings = current or FinanceBankingSettings(entity=entity)
    before_all = serialize_finance_banking_settings(settings)
    changed = False
    for field, value in values.items():
        if getattr(settings, field) != value:
            setattr(settings, field, value)
            changed = True
    if changed:
        settings.updated_by = actor_user
        settings.full_clean()
        settings.save()
    after_all = serialize_finance_banking_settings(settings)
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
            action=FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED,
            actor_user=actor_user,
            target=settings if settings.pk else None,
            target_type="FinanceBankingSettings",
            target_id=str(settings.pk or entity.pk),
            message=f"Updated {len(changed_after)} finance banking setting(s).",
            before=changed_before,
            after=changed_after,
        )
    return settings
