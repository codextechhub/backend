"""Procurement's transaction models, as the branch backfill derives them.

Discovered by :mod:`vs_finance.branch_derivation` on first use. A purchase
document reads the document it came from: the RFQ its requisition, the order
its requisition or the quotation it awarded, the receipt and the bill their
order. A requisition reads the person who raised it. A supplier payment reads
the bills it settles, when they agree, then the bank it was paid from.

Order numbers sit between finance's documents and its journals, so every
journal a procurement document raised can read that document's branch.
"""
from __future__ import annotations

from vs_finance.branch_derivation import (
    JournalOwner,
    Target,
    agreeing,
    bank_behind,
    register_journal_owner,
    register_target,
    user_branch,
    via,
)

REQUISITION = "vs_procurement.PurchaseRequisition"
RFQ = "vs_procurement.RequestForQuotation"
QUOTATION = "vs_procurement.VendorQuotation"
PURCHASE_ORDER = "vs_procurement.PurchaseOrder"
GRN = "vs_procurement.GoodsReceivedNote"
BILL = "vs_procurement.VendorInvoice"
VENDOR_PAYMENT = "vs_procurement.VendorPayment"
STOCK_LOCATION = "vs_procurement.StockLocation"

_TARGETS = (
    Target(REQUISITION, (user_branch("the requester", "requested_by"),), order=100, audit_module="PROCUREMENT"),
    Target(RFQ, (via("the requisition", "requisition", REQUISITION),), order=110, audit_module="PROCUREMENT"),
    Target(QUOTATION, (via("the RFQ", "rfq", RFQ),), order=120, audit_module="PROCUREMENT"),
    Target(
        PURCHASE_ORDER,
        (
            via("the requisition", "requisition", REQUISITION),
            agreeing("the awarded quotation", QUOTATION, "awarded_po", ("id", QUOTATION)),
        ),
        order=130, audit_module="PROCUREMENT",
    ),
    Target(GRN, (via("the purchase order", "purchase_order", PURCHASE_ORDER),), order=140, audit_module="PROCUREMENT"),
    Target(BILL, (via("the purchase order", "purchase_order", PURCHASE_ORDER),), order=140, audit_module="PROCUREMENT"),
    Target(
        VENDOR_PAYMENT,
        (
            agreeing("the settled bills", "vs_procurement.VendorPaymentAllocation", "payment", ("vendor_invoice", BILL)),
            bank_behind("the paying bank account", "payment_account"),
        ),
        order=150, audit_module="PROCUREMENT",
    ),
    Target(
        STOCK_LOCATION, (), order=100, audit_module="PROCUREMENT",
        no_source_note="a central store belongs to one branch; an administrator decides which",
    ),
)

#: Every procurement model holding a foreign key to the journal it raised.
_JOURNAL_OWNERS = (
    JournalOwner(GRN, "journal"),
    JournalOwner(BILL, "journal"),
    JournalOwner(VENDOR_PAYMENT, "journal"),
    JournalOwner("vs_procurement.StockMovement", "journal", via="location", via_label=STOCK_LOCATION),
    JournalOwner("vs_procurement.VendorAdvanceAllocationJournal", "journal", via="payment", via_label=VENDOR_PAYMENT),
)

for _target in _TARGETS:
    register_target(_target)
for _owner in _JOURNAL_OWNERS:
    register_journal_owner(_owner)
