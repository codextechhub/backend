"""Typed entity-level account mappings and safe account-role resolution."""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import (
    AccountMappingKey,
    AccountType,
    FinanceAuditAction,
)
from .exceptions import MissingAccountError


ACCOUNT_MAPPING_SPECS = {
    AccountMappingKey.CASH_BANK: ("1100", AccountType.ASSET),
    AccountMappingKey.ACCOUNTS_RECEIVABLE: ("1200", AccountType.ASSET),
    AccountMappingKey.ACCOUNTS_PAYABLE: ("2100", AccountType.LIABILITY),
    AccountMappingKey.CUSTOMER_CREDIT: ("2140", AccountType.LIABILITY),
    # The counterparty mirror of 2140, and deliberately an ASSET: money paid to a
    # vendor before their bill exists is value the vendor still owes us in goods.
    AccountMappingKey.VENDOR_ADVANCE: ("1240", AccountType.ASSET),
    AccountMappingKey.GRIR_CLEARING: ("2150", AccountType.LIABILITY),
    AccountMappingKey.OUTPUT_VAT: ("2200", AccountType.LIABILITY),
    AccountMappingKey.WHT_PAYABLE: ("2300", AccountType.LIABILITY),
    AccountMappingKey.RETAINED_EARNINGS: ("3200", AccountType.EQUITY),
    AccountMappingKey.BAD_DEBT_EXPENSE: ("5300", AccountType.EXPENSE),
    AccountMappingKey.BANK_CHARGES: ("5500", AccountType.EXPENSE),
    AccountMappingKey.INVENTORY_ASSET: ("1400", AccountType.ASSET),
    AccountMappingKey.INVENTORY_ADJUSTMENT: ("5150", AccountType.EXPENSE),
    AccountMappingKey.PURCHASE_PRICE_VARIANCE: ("5160", AccountType.EXPENSE),
}

DEFAULT_CODE_TO_MAPPING_KEY = {
    code: key for key, (code, _account_type) in ACCOUNT_MAPPING_SPECS.items()
}


def _usable(account, expected_type):
    return bool(
        account
        and account.account_type == expected_type
        and account.is_active
        and account.is_postable
    )


def resolve_mapped_account(entity, key, *, label=""):
    """Resolve an active, postable account for a stable entity account role."""
    from .models import Account, FinanceAccountMapping

    try:
        default_code, expected_type = ACCOUNT_MAPPING_SPECS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown account mapping key '{key}'.") from exc

    mapping = (
        FinanceAccountMapping.objects.filter(entity=entity, key=key)
        .select_related("account").first()
    )
    account = mapping.account if mapping else (
        Account.objects.filter(entity=entity, code=default_code).first()
    )
    if not _usable(account, expected_type):
        code = getattr(account, "code", None) or default_code
        raise MissingAccountError(code, label=label or AccountMappingKey(key).label)
    return account


def resolve_default_code_mapping(entity, code, *, label=""):
    """Resolve a mapped role when ``code`` is a known default, otherwise by code."""
    from .accounts import resolve_account

    key = DEFAULT_CODE_TO_MAPPING_KEY.get(str(code))
    if key:
        return resolve_mapped_account(entity, key, label=label)
    return resolve_account(entity, code, label=label)


def account_mapping_snapshot(entity):
    """Return every mapping role with its effective account and source."""
    from .models import Account, FinanceAccountMapping

    mappings = {
        row.key: row for row in FinanceAccountMapping.objects.filter(entity=entity)
        .select_related("account")
    }
    default_codes = {code for code, _account_type in ACCOUNT_MAPPING_SPECS.values()}
    defaults = {
        account.code: account
        for account in Account.objects.filter(entity=entity, code__in=default_codes)
    }
    rows = []
    for key, (default_code, expected_type) in ACCOUNT_MAPPING_SPECS.items():
        mapping = mappings.get(key)
        account = mapping.account if mapping else defaults.get(default_code)
        rows.append({
            "key": key,
            "label": AccountMappingKey(key).label,
            "expected_account_type": expected_type,
            "default_code": default_code,
            "source": "OVERRIDE" if mapping else "DEFAULT",
            "account": ({
                "id": account.id,
                "code": account.code,
                "name": account.name,
                "account_type": account.account_type,
                "is_active": account.is_active,
                "is_postable": account.is_postable,
            } if account else None),
            "is_valid": _usable(account, expected_type),
        })
    return rows


def account_mapping_options(entity):
    """Return selectable accounts without requiring a second broad chart read."""
    from .models import Account

    return list(
        Account.objects.filter(entity=entity, is_active=True, is_postable=True)
        .order_by("code")
        .values("id", "code", "name", "account_type")
    )


def _codes_by_key(entity):
    return {
        row["key"]: row["account"]["code"] if row["account"] else None
        for row in account_mapping_snapshot(entity)
    }


@transaction.atomic
def update_account_mappings(*, entity, values, actor_user):
    """Apply a partial mapping update and audit the effective before/after codes."""
    from .models import Account, FinanceAccountMapping

    if not isinstance(values, dict) or not values:
        raise ValidationError({"mappings": "Provide at least one account mapping."})
    unknown = sorted(set(values) - set(ACCOUNT_MAPPING_SPECS))
    if unknown:
        raise ValidationError({"mappings": f"Unknown account mapping keys: {', '.join(unknown)}."})

    before = _codes_by_key(entity)
    for key, reference in values.items():
        if reference in (None, ""):
            FinanceAccountMapping.objects.filter(entity=entity, key=key).delete()
            continue
        account_qs = Account.objects.filter(entity=entity)
        account = account_qs.filter(pk=int(reference)).first() if str(reference).isdigit() else (
            account_qs.filter(code=str(reference).strip()).first()
        )
        if account is None:
            raise ValidationError({"mappings": {key: "No such account in this entity."}})
        _default_code, expected_type = ACCOUNT_MAPPING_SPECS[key]
        if not _usable(account, expected_type):
            raise ValidationError({
                "mappings": {
                    key: f"Select an active, postable {expected_type.lower()} account.",
                },
            })
        FinanceAccountMapping.objects.update_or_create(
            entity=entity, key=key,
            defaults={"account": account, "updated_by": actor_user},
        )

    after = _codes_by_key(entity)
    changed_before = {key: before[key] for key in values if before[key] != after[key]}
    changed_after = {key: after[key] for key in values if before[key] != after[key]}
    if changed_after:
        record(
            entity=entity,
            action=FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
            actor_user=actor_user,
            target_type="FinanceAccountSettings",
            target_id=str(entity.pk),
            message=f"Updated {len(changed_after)} finance account mapping(s).",
            before=changed_before,
            after=changed_after,
        )
    return account_mapping_snapshot(entity)
