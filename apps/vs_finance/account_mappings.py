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
    AccountMappingKey.BAD_DEBT_EXPENSE: ("5350", AccountType.EXPENSE),
    AccountMappingKey.BANK_CHARGES: ("5500", AccountType.EXPENSE),
    AccountMappingKey.INVENTORY_ASSET: ("1400", AccountType.ASSET),
    AccountMappingKey.INVENTORY_ADJUSTMENT: ("5150", AccountType.EXPENSE),
    AccountMappingKey.PURCHASE_PRICE_VARIANCE: ("5160", AccountType.EXPENSE),
    # Fees billed before their service period starts wait here until each month
    # they belong to releases them to revenue.
    AccountMappingKey.DEFERRED_INCOME: ("2160", AccountType.LIABILITY),
    # Refundable deposits are money held for the customer, never revenue.
    AccountMappingKey.DEPOSITS_HELD: ("2170", AccountType.LIABILITY),
    # A contra-asset, typed ASSET like 1900 Accumulated Depreciation: it carries a
    # credit balance that nets against the receivables it provides for.
    AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE: ("1290", AccountType.ASSET),
    AccountMappingKey.BAD_DEBT_RECOVERED: ("4810", AccountType.INCOME),
    AccountMappingKey.FORFEITED_DEPOSIT_INCOME: ("4820", AccountType.INCOME),
    # Money a payment provider has confirmed that has not yet reached a bank. It
    # empties as each settlement lands, so a balance here is money in transit.
    AccountMappingKey.GATEWAY_CLEARING: ("1125", AccountType.ASSET),
    # The platform's own books: money its provider balance holds for clients
    # (a liability, one sub-ledger row per client branch) and that balance.
    AccountMappingKey.CLIENT_FUNDS_HELD: ("2180", AccountType.LIABILITY),
    AccountMappingKey.PROVIDER_BALANCE: ("1127", AccountType.ASSET),
    # What a client branch owes the platform once a chargeback took more than it
    # held: the debit side of its held balance, kept apart from the liability.
    AccountMappingKey.CLIENT_FUNDS_OWED: ("1128", AccountType.ASSET),
    # A held payment its payer's bank took back, the branch's loss.
    AccountMappingKey.CHARGEBACKS: ("5520", AccountType.EXPENSE),
}

#: Roles only the platform's books carry. Every other set of books neither lists
#: nor maps them: a tenant holds no money for other tenants.
PLATFORM_ONLY_MAPPING_KEYS = frozenset({
    AccountMappingKey.CLIENT_FUNDS_HELD,
    AccountMappingKey.PROVIDER_BALANCE,
    AccountMappingKey.CLIENT_FUNDS_OWED,
})


def mapping_keys_for(entity):
    """The account roles ``entity``'s books list and may map."""
    if getattr(entity, "is_platform", False):
        return list(ACCOUNT_MAPPING_SPECS)
    return [key for key in ACCOUNT_MAPPING_SPECS if key not in PLATFORM_ONLY_MAPPING_KEYS]


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
    for key in mapping_keys_for(entity):
        default_code, expected_type = ACCOUNT_MAPPING_SPECS[key]
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
def update_account_mappings(*, request, entity, values, actor_user):
    """Apply a partial mapping update and audit the effective before/after codes.

    Each account is named under the caller's bank reach (see
    :func:`vs_finance.accounts.accounts_a_caller_may_name`), so a mapping cannot
    point the school's defaults at another branch's bank ledger that the caller
    could not name on a document.
    """
    from .accounts import accounts_a_caller_may_name
    from .models import Account, FinanceAccountMapping

    if not isinstance(values, dict) or not values:
        raise ValidationError({"mappings": "Provide at least one account mapping."})
    unknown = sorted(set(values) - set(mapping_keys_for(entity)))
    if unknown:
        raise ValidationError({"mappings": f"Unknown account mapping keys: {', '.join(unknown)}."})

    before = _codes_by_key(entity)
    for key, reference in values.items():
        if reference in (None, ""):
            FinanceAccountMapping.objects.filter(entity=entity, key=key).delete()
            continue
        account_qs = accounts_a_caller_may_name(request, Account.objects.filter(entity=entity))
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
