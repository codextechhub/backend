"""Typed customer-document defaults and audited entity policy updates."""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import FinanceAuditAction
from .models import BankAccount, FinanceDocumentSettings


SETTING_FIELDS = (
    "default_invoice_due_days",
    "default_invoice_narration",
    "auto_post_manual_invoices",
    "allow_customer_opening_balances",
    "primary_collection_bank_account",
)


def resolve_finance_document_settings(entity):
    settings = FinanceDocumentSettings.objects.filter(entity=entity).first()
    return settings or FinanceDocumentSettings(entity=entity)


def _primary_collection(entity):
    return BankAccount.objects.filter(
        entity=entity, is_primary_collection=True,
    ).select_related("currency", "entity").first()


def _bank_summary(bank):
    if bank is None:
        return None
    return {
        "id": bank.id,
        "name": bank.name,
        "bank_name": bank.bank_name,
        "currency": bank.currency_id or bank.entity.base_currency_id,
    }


def serialize_finance_document_settings(settings):
    entity = settings.entity
    primary = _primary_collection(entity)
    return {
        "default_invoice_due_days": settings.default_invoice_due_days,
        "default_invoice_narration": settings.default_invoice_narration,
        "auto_post_manual_invoices": settings.auto_post_manual_invoices,
        "allow_customer_opening_balances": settings.allow_customer_opening_balances,
        "primary_collection_bank_account": _bank_summary(primary),
        "bank_account_options": [
            _bank_summary(bank) for bank in BankAccount.objects.filter(
                entity=entity, is_active=True,
            ).select_related("currency", "entity").order_by("name")
        ],
        "updated_at": settings.updated_at.isoformat() if settings.pk else None,
        "updated_by": settings.updated_by.email if settings.pk and settings.updated_by else None,
    }


def _validated_values(data):
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one document setting."})
    unknown = sorted(set(data) - set(SETTING_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    values = {}
    if "default_invoice_due_days" in data:
        value = data["default_invoice_due_days"]
        if isinstance(value, bool):
            raise ValidationError({"default_invoice_due_days": "Enter a whole number of days."})
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"default_invoice_due_days": "Enter a whole number of days."}) from exc
        if value < 0 or value > 365:
            raise ValidationError({"default_invoice_due_days": "Use a value from 0 to 365 days."})
        values["default_invoice_due_days"] = value
    if "default_invoice_narration" in data:
        value = str(data["default_invoice_narration"] or "").strip()
        if len(value) > 255:
            raise ValidationError({"default_invoice_narration": "Use 255 characters or fewer."})
        values["default_invoice_narration"] = value
    for field in ("auto_post_manual_invoices", "allow_customer_opening_balances"):
        if field not in data:
            continue
        if not isinstance(data[field], bool):
            raise ValidationError({field: "Use true or false."})
        values[field] = data[field]
    if "primary_collection_bank_account" in data:
        values["primary_collection_bank_account"] = data["primary_collection_bank_account"]
    return values


@transaction.atomic
def update_finance_document_settings(*, entity, data, actor_user):
    """Apply a partial document-policy update and audit only effective changes."""
    values = _validated_values(data)
    current = FinanceDocumentSettings.objects.select_for_update().filter(entity=entity).first()
    settings = current or FinanceDocumentSettings(entity=entity)
    before = serialize_finance_document_settings(settings)

    model_changed = False
    for field in SETTING_FIELDS[:-1]:
        if field in values and getattr(settings, field) != values[field]:
            setattr(settings, field, values[field])
            model_changed = True
    if model_changed:
        settings.updated_by = actor_user
        settings.full_clean()
        settings.save()

    if "primary_collection_bank_account" in values:
        reference = values["primary_collection_bank_account"]
        banks = BankAccount.objects.select_for_update().filter(entity=entity)
        selected = None
        if reference not in (None, ""):
            selected = banks.filter(pk=reference, is_active=True).first()
            if selected is None:
                raise ValidationError({
                    "primary_collection_bank_account": "Select an active bank account in this entity.",
                })
        banks.filter(is_primary_collection=True).exclude(
            pk=selected.pk if selected else None,
        ).update(is_primary_collection=False)
        if selected and not selected.is_primary_collection:
            selected.is_primary_collection = True
            selected.save(update_fields=["is_primary_collection", "updated_at"])

    after = serialize_finance_document_settings(settings)
    changed_before = {field: before[field] for field in values if before[field] != after[field]}
    changed_after = {field: after[field] for field in values if before[field] != after[field]}
    if changed_after:
        record(
            entity=entity,
            action=FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED,
            actor_user=actor_user,
            target_type="FinanceDocumentSettings",
            target_id=str(settings.pk or entity.pk),
            message=f"Updated {len(changed_after)} finance document setting(s).",
            before=changed_before,
            after=changed_after,
        )
    return settings
