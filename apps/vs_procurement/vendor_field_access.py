"""Vendor values that travel inside summaries, reports and dashboards.

A figure that counts vendors by hold, by KYC status or by category, or that
labels a supplier with its category or terms, says something about a vendor
field as surely as the vendor's own row does. The vendor serializers withhold
those fields from a role whose Read switch is off; this module holds the same
caller to the same switch for the numbers and labels built from them, so a
clerk who cannot see which suppliers are on hold cannot read it off a count.

Each function takes the request of the person asking and returns the payload
with the withheld values removed. A block that is a breakdown by a hidden field
(spend by category) goes away whole, since what is left of it would be a total
presented as a breakdown.
"""
from __future__ import annotations

from vs_rbac.field_enforcement import FieldReadDenied, can_read

_VENDOR = "procurement.vendor"


def vendor_field_readable(request, name: str) -> bool:
    """Whether the caller may read the vendor field registered as *name*."""
    return can_read(request, f"{_VENDOR}.{name}")


def require_vendor_field(request, name: str, label: str) -> None:
    """Refuse a request that filters or groups by a vendor value the caller may not read."""
    if not vendor_field_readable(request, name):
        raise FieldReadDenied([name], labels={name: label})


def trim_overview_dashboard(request, data: dict) -> dict:
    """The procurement overview, without the vendor values the caller may not read."""
    if not vendor_field_readable(request, "category"):
        data["spend_by_category"] = None
    if not vendor_field_readable(request, "on_hold"):
        vendors = (data.get("kpis") or {}).get("active_vendors")
        if vendors:
            vendors.pop("on_hold_count", None)
        if data.get("exceptions") is not None:
            data["exceptions"] = [
                row for row in data["exceptions"] if row.get("key") != "vendor_on_hold"
            ]
    return data


def trim_suppliers_dashboard(request, data: dict) -> dict:
    """The spend and suppliers tab, without the vendor values the caller may not read."""
    if not vendor_field_readable(request, "category"):
        for row in data.get("scorecard") or []:
            row.pop("category", None)
        if data.get("savings") is not None:
            data["savings"]["items"] = []
    base = data.get("vendor_base")
    if base:
        if not vendor_field_readable(request, "on_hold"):
            base.pop("on_hold", None)
        if not vendor_field_readable(request, "kyc_status"):
            base.pop("awaiting_kyc", None)
    return data
