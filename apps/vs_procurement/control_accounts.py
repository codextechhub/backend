"""The accounts procurement keeps, contributed to finance's hand-journal lock.

Finance locks the accounts its own mappings and rows name
(:mod:`vs_finance.control_accounts`). Procurement adds the two it owns through its
own rows: each vendor's payable account and each stock item's inventory account.
Registered from ``AppConfig.ready`` so finance never imports procurement.
"""
from __future__ import annotations

_PAYABLES = (
    "the payables ledger",
    "a vendor bill, a vendor payment, a vendor credit note or an opening bill",
)
_STOCK = ("the stock ledger", "a goods receipt, a stock issue or a stock adjustment")


def procurement_control_accounts(entity) -> dict:
    """``{account_id: (kept_by, use_instead)}`` for vendor payables and stock items."""
    from .models import StockItem, Vendor

    kept = {}
    for account_id in Vendor.objects.filter(entity=entity).exclude(
            payable_account=None).values_list("payable_account_id", flat=True).distinct():
        kept.setdefault(account_id, _PAYABLES)
    for account_id in StockItem.objects.filter(entity=entity).values_list(
            "inventory_account_id", flat=True).distinct():
        kept.setdefault(account_id, _STOCK)
    return kept


def register():
    """Contribute procurement's accounts to the lock. Called from AppConfig.ready."""
    from vs_finance.control_accounts import register_control_account_provider

    register_control_account_provider(procurement_control_accounts)
