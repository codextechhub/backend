"""The procurement screens and settings the search bar can route to.

Each setting is built from the tables the procurement settings screen already
serves: the label from :data:`vs_procurement.settings.HISTORY_FIELDS` and the
description from :data:`vs_procurement.settings_ownership.PROCUREMENT_SETTING_CONSUMERS`,
whose service name is carried as a keyword. A setting added to the screen is
therefore searchable with it, in the same words.

Procurement sits behind the ``procurement`` capability (:mod:`vs_rbac.capability_map`).

The procurement overview opens to anybody holding any procurement key, as
:class:`vs_rbac.permissions.HasAnyModuleAccess` lets its endpoint, so its place
names the ``procurement.member`` condition rather than one key.
"""
from __future__ import annotations

from vs_search.registry import ACTION, SETTING, Place, register, register_condition

from .settings import HISTORY_FIELDS
from .settings_ownership import PROCUREMENT_SETTING_CONSUMERS

CAPABILITY = "procurement"
PROCUREMENT_MEMBER = "procurement.member"


def is_procurement_member(*, request, tenant, branch, user) -> bool:
    """Whether *user* holds any procurement key, as the overview's door asks."""
    from vs_rbac.permissions import holds_any_module_key

    permission_tenant = (
        getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
        or getattr(user, "tenant", None)
    )
    return holds_any_module_key(user, permission_tenant, ("procurement",))


register_condition(PROCUREMENT_MEMBER, is_procurement_member)
SETTINGS_SCREEN = "procurement.settings"
SETTINGS_VIEW = "procurement.settings.view"
SETTINGS_GROUP = "Procurement settings"

#: Words a person types that a setting's label does not contain, by field.
EXTRA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "default_rfq_response_days": ("rfq", "quotation deadline"),
    "rfq_closing_soon_days": ("rfq",),
    "minimum_rfq_invited_vendors": ("rfq",),
}

#: (resource, label, description, permission) for each top-level procurement list screen.
LIST_SCREENS = (
    ("vendors", "Vendors", "Suppliers and their checks.", "procurement.vendor.view"),
    ("requisitions", "Requisitions", "Requests to buy.", "procurement.requisition.view"),
    ("rfqs", "Requests for quotation", "", "procurement.rfq.view"),
    ("purchase_orders", "Purchase orders", "", "procurement.purchase_order.view"),
    ("goods_receipts", "Goods receipts", "", "procurement.goods_receipt.view"),
    ("vendor_invoices", "Vendor invoices", "Bills received from vendors.",
     "procurement.vendor_invoice.view"),
    ("vendor_payments", "Vendor payments", "", "procurement.vendor_payment.view"),
    ("contracts", "Contracts", "", "procurement.contract.view"),
    ("stock", "Stock items", "Stores, balances and movements.", "procurement.stock.view"),
    ("stock_movements", "Stock movements", "Every receipt, issue and transfer of stock.",
     "procurement.stock.view"),
    ("stock_locations", "Stock locations", "The stores stock is kept in.", "procurement.stock.view"),
    ("categories", "Vendor categories", "", "procurement.category.view"),
    ("catalog", "Catalogue", "Items bought often, with their prices.",
     "procurement.catalog_item.view"),
    ("quotations", "Quotations", "Vendors' answers to requests for quotation.",
     "procurement.quotation.view"),
    ("ap_aging", "Unpaid bills by age", "What is owed to vendors and for how long.",
     "procurement.analytics.view"),
    ("grir", "Goods not yet billed", "Goods received that no vendor invoice covers yet.",
     "procurement.analytics.view"),
    ("spend", "Spend analysis", "Spending by vendor, category and period.",
     "procurement.analytics.view"),
    ("vendor_performance", "Vendor performance", "Delivery, quality and price per vendor.",
     "procurement.analytics.view"),
)


def places():
    """Every procurement place, the settings screen first."""
    yield Place(
        kind=ACTION, key=SETTINGS_SCREEN, label=SETTINGS_GROUP,
        description="Purchasing defaults and invoice-matching tolerances.",
        screen=SETTINGS_SCREEN, permission=SETTINGS_VIEW, capability=CAPABILITY,
    )
    for name, spec in HISTORY_FIELDS.items():
        consumer = PROCUREMENT_SETTING_CONSUMERS.get(name) or {}
        yield Place(
            kind=SETTING,
            key=f"{SETTINGS_SCREEN}.{name}",
            label=spec.label,
            description=consumer.get("impact", ""),
            keywords=tuple(filter(None, (consumer.get("service"),)))
            + EXTRA_KEYWORDS.get(name, ()),
            group=SETTINGS_GROUP,
            screen=SETTINGS_SCREEN,
            anchor=name,
            permission=SETTINGS_VIEW,
            capability=CAPABILITY,
        )
    for resource, label, description, permission in LIST_SCREENS:
        yield Place(
            kind=ACTION, key=f"procurement.{resource}", label=label,
            description=description, screen=f"procurement.{resource}",
            permission=permission, capability=CAPABILITY,
        )
    yield Place(
        kind=ACTION, key="procurement.dashboard", label="Procurement overview",
        description="The procurement console's landing page.",
        keywords=("dashboard", "procurement home"), screen="procurement.dashboard",
        capability=CAPABILITY, condition=PROCUREMENT_MEMBER,
    )


register(*places())
