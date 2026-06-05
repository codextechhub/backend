"""Procurement models (vs_procurement).

Phase 0 establishes the app and its dependency direction only: **procurement
depends on finance, never the reverse.** Concrete models — Vendor, CatalogItem,
PurchaseRequisition, RFQ/Quotation, PurchaseOrder, GoodsReceivedNote, VendorInvoice,
VendorPayment, Contract — land in Phase 3 and will build on the shared foundations
imported below (the abstract numbered-document base, the money field, the document
sequence). Importing them here both documents the intended dependency and guarantees
finance is import-safe from procurement.
"""
from __future__ import annotations

from vs_finance.models import FinanceDocument  # noqa: F401  (Phase-3 base)
from vs_finance.money import MoneyField  # noqa: F401  (Phase-3 monetary columns)

# No concrete procurement models yet — see Phase 3 of the build plan.
