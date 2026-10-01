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
    "term_collection_target_pct",
    "auto_apply_customer_credit",
    "concession_second_person_threshold",
    # Not a model field; kept last because the update loop skips it.
    "primary_collection_bank_account",
)


def resolve_finance_document_settings(entity):
    settings = FinanceDocumentSettings.objects.filter(entity=entity).first()
    return settings or FinanceDocumentSettings(entity=entity)


def _primary_collections(entity):
    """Every flagged collection account of ``entity``, one per branch, branch order."""
    return list(
        BankAccount.objects.filter(entity=entity, is_primary_collection=True)
        .select_related("currency", "entity", "branch").order_by("branch_id", "id")
    )


def _bank_summary(bank):
    if bank is None:
        return None
    return {
        "id": bank.id,
        "name": bank.name,
        "bank_name": bank.bank_name,
        "currency": bank.currency_id or bank.entity.base_currency_id,
        "branch": bank.branch_id,
        "branch_name": bank.branch.name if bank.branch_id else "",
    }


def serialize_finance_document_settings(settings):
    """The entity's document settings, with each branch's collection account.

    Every branch prints its own collection account as "pay to"
    (:func:`vs_finance.documents.primary_collection_account`), so
    ``primary_collection_bank_accounts`` lists one per branch that has one.
    ``primary_collection_bank_account`` is the first of them, which at a tenant
    with one branch is the only one.
    """
    entity = settings.entity
    primaries = _primary_collections(entity)
    return {
        "default_invoice_due_days": settings.default_invoice_due_days,
        "default_invoice_narration": settings.default_invoice_narration,
        "auto_post_manual_invoices": settings.auto_post_manual_invoices,
        "allow_customer_opening_balances": settings.allow_customer_opening_balances,
        "term_collection_target_pct": settings.term_collection_target_pct,
        "auto_apply_customer_credit": settings.auto_apply_customer_credit,
        "concession_second_person_threshold": settings.concession_second_person_threshold,
        "primary_collection_bank_account": _bank_summary(primaries[0] if primaries else None),
        "primary_collection_bank_accounts": [_bank_summary(bank) for bank in primaries],
        "bank_account_options": [
            _bank_summary(bank) for bank in BankAccount.objects.filter(
                entity=entity, is_active=True,
            ).select_related("currency", "entity", "branch").order_by("name")
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
    for field in ("auto_post_manual_invoices", "allow_customer_opening_balances",
                  "auto_apply_customer_credit"):
        if field not in data:
            continue
        if not isinstance(data[field], bool):
            raise ValidationError({field: "Use true or false."})
        values[field] = data[field]
    if "term_collection_target_pct" in data:
        value = data["term_collection_target_pct"]
        try:
            if isinstance(value, bool):
                raise TypeError
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"term_collection_target_pct": "Enter a whole percentage."}) from exc
        if value < 1 or value > 100:
            raise ValidationError({"term_collection_target_pct": "Use a value from 1 to 100."})
        values["term_collection_target_pct"] = value
    if "concession_second_person_threshold" in data:
        field = "concession_second_person_threshold"
        value = data[field]
        try:
            if isinstance(value, bool):
                raise TypeError
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Enter a whole amount in kobo."}) from exc
        if value < 0:
            raise ValidationError({field: "Use zero or a positive amount in kobo."})
        values[field] = value
    if "primary_collection_bank_account" in data:
        values["primary_collection_bank_account"] = data["primary_collection_bank_account"]
    return values


def _set_collection_account(entity, reference):
    """Make ``reference`` its branch's collection account; return ``(before, after)`` summaries.

    Each branch has one collection account, so choosing Lekki's Zenith account
    replaces Lekki's previous choice and leaves Ikeja's GTBank as it is. A blank
    reference clears every branch's choice, after which each document prints its
    branch's first active account.
    """
    banks = BankAccount.objects.select_for_update().filter(entity=entity)
    if reference in (None, ""):
        previous = _primary_collections(entity)
        banks.filter(is_primary_collection=True).update(is_primary_collection=False)
        return (_bank_summary(previous[0]) if previous else None), None
    selected = banks.filter(pk=reference, is_active=True).first()
    if selected is None:
        raise ValidationError({
            "primary_collection_bank_account": "Select an active bank account in this entity.",
        })
    previous = (
        BankAccount.objects.filter(
            entity=entity, branch_id=selected.branch_id, is_primary_collection=True,
        ).select_related("currency", "entity", "branch").first()
    )
    banks.filter(branch_id=selected.branch_id, is_primary_collection=True).exclude(
        pk=selected.pk).update(is_primary_collection=False)
    if not selected.is_primary_collection:
        selected.is_primary_collection = True
        selected.save(update_fields=["is_primary_collection", "updated_at"])
    return _bank_summary(previous), _bank_summary(selected)


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

    collection_change = None
    if "primary_collection_bank_account" in values:
        collection_change = _set_collection_account(
            entity, values["primary_collection_bank_account"])

    after = serialize_finance_document_settings(settings)
    if collection_change is not None:  # Audit the branch whose pay-to account moved.
        before["primary_collection_bank_account"], after["primary_collection_bank_account"] = (
            collection_change)
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
