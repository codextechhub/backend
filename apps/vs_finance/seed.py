"""Default reference data: currencies and a starter Chart of Accounts.

The CoA here is a deliberately small, domain-neutral skeleton — the five roots and
the handful of control accounts every entity needs (cash, AR, AP, VAT, share capital,
retained earnings, a generic income and expense). Product adapters (school fees,
payroll …) extend it; nothing here mentions students or schools, honouring the
horizontal-module rule.
"""
from __future__ import annotations

from .constants import AccountType

#: ISO currencies the platform knows out of the box. NGN is the platform base.
DEFAULT_CURRENCIES = [
    {"code": "NGN", "name": "Nigerian Naira", "symbol": "₦", "minor_unit": 2},
    {"code": "USD", "name": "US Dollar", "symbol": "$", "minor_unit": 2},
    {"code": "GBP", "name": "Pound Sterling", "symbol": "£", "minor_unit": 2},
    {"code": "EUR", "name": "Euro", "symbol": "€", "minor_unit": 2},
]

#: (code, name, type, is_postable, is_contra). Header rows (is_postable=False) give
#: the tree its sections; leaves take postings.
DEFAULT_CHART = [
    # Assets
    ("1000", "Assets", AccountType.ASSET, False, False),
    ("1100", "Cash & Bank", AccountType.ASSET, True, False),
    ("1200", "Accounts Receivable", AccountType.ASSET, True, False),
    ("1300", "Input VAT (Recoverable)", AccountType.ASSET, True, False),
    ("1900", "Accumulated Depreciation", AccountType.ASSET, True, True),
    # Liabilities
    ("2000", "Liabilities", AccountType.LIABILITY, False, False),
    ("2100", "Accounts Payable", AccountType.LIABILITY, True, False),
    ("2150", "GR/IR Clearing", AccountType.LIABILITY, True, False),
    ("2200", "Output VAT (Payable)", AccountType.LIABILITY, True, False),
    ("2300", "WHT Payable", AccountType.LIABILITY, True, False),
    # Equity
    ("3000", "Equity", AccountType.EQUITY, False, False),
    ("3100", "Share Capital", AccountType.EQUITY, True, False),
    ("3200", "Retained Earnings", AccountType.EQUITY, True, False),
    # Income
    ("4000", "Income", AccountType.INCOME, False, False),
    ("4100", "Operating Revenue", AccountType.INCOME, True, False),
    ("4900", "Sales Returns & Allowances", AccountType.INCOME, True, True),
    # Expenses
    ("5000", "Expenses", AccountType.EXPENSE, False, False),
    ("5100", "Cost of Sales", AccountType.EXPENSE, True, False),
    ("5200", "Salaries & Wages", AccountType.EXPENSE, True, False),
    ("5300", "General & Administrative", AccountType.EXPENSE, True, False),
]

#: parent_code by child_code — wires the tree after the flat create.
_PARENTS = {
    "1100": "1000", "1200": "1000", "1300": "1000", "1900": "1000",
    "2100": "2000", "2150": "2000", "2200": "2000", "2300": "2000",
    "3100": "3000", "3200": "3000",
    "4100": "4000", "4900": "4000",
    "5100": "5000", "5200": "5000", "5300": "5000",
}


def seed_currencies():
    """Create the default currencies (idempotent). Returns the count touched."""
    from .models import Currency

    for spec in DEFAULT_CURRENCIES:
        Currency.objects.update_or_create(code=spec["code"], defaults=spec)
    return len(DEFAULT_CURRENCIES)


def seed_chart_of_accounts(entity):
    """Create the default Chart of Accounts for ``entity`` (idempotent per code).

    Safe to re-run: accounts are keyed by ``(entity, code)`` and only created when
    absent. Returns the list of :class:`~vs_finance.models.Account` rows for the
    entity after seeding.
    """
    from .models import Account

    created: dict[str, Account] = {}
    for code, name, acc_type, postable, contra in DEFAULT_CHART:
        # ``normal_balance`` is left for Account.save() to derive from type + contra.
        account, _ = Account.objects.get_or_create(
            entity=entity, code=code,
            defaults={
                "name": name,
                "account_type": acc_type,
                "is_postable": postable,
                "is_contra": contra,
            },
        )
        created[code] = account

    # Second pass: link parents now that every node exists.
    for child_code, parent_code in _PARENTS.items():
        child = created.get(child_code) or Account.objects.filter(entity=entity, code=child_code).first()
        parent = created.get(parent_code) or Account.objects.filter(entity=entity, code=parent_code).first()
        if child and parent and child.parent_id != parent.id:
            child.parent = parent
            child.save(update_fields=["parent", "updated_at"])

    return list(Account.objects.filter(entity=entity).order_by("code"))
