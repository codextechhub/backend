"""Enumerations and well-known account codes for the procurement engine (Phase 3).

Procurement depends on finance, never the reverse, so procurement-specific vocabulary
lives here — not in :mod:`vs_finance.constants`. The few *audit* actions are the
exception: they belong to finance's authoritative log and are named in
``vs_finance.constants.FinanceAuditAction`` (string constants only; no import cycle).

The ``*_ACCOUNT_CODE`` constants below name the control accounts the P2P journals
post to. They are resolved by code against the entity's Chart of Accounts at posting
time (see :func:`vs_procurement.purchasing.resolve_account`), and are seeded by
:data:`vs_finance.seed.DEFAULT_CHART`.
"""
from __future__ import annotations

from django.db import models


class VendorKycStatus(models.TextChoices):
    """Know-your-vendor verification state (compliance gate before paying)."""
    PENDING = "PENDING", "Pending"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"


class VendorRisk(models.TextChoices):
    """Coarse risk grade used to route approvals and flag exposure."""
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class PaymentTerms(models.TextChoices):
    """Standard net payment terms; the integer days drive the invoice due date."""
    IMMEDIATE = "NET_0", "Due on receipt"
    NET_7 = "NET_7", "Net 7 days"
    NET_14 = "NET_14", "Net 14 days"
    NET_30 = "NET_30", "Net 30 days"
    NET_60 = "NET_60", "Net 60 days"
    NET_90 = "NET_90", "Net 90 days"


#: Days implied by each :class:`PaymentTerms` value (for due-date arithmetic).
PAYMENT_TERM_DAYS = {
    PaymentTerms.IMMEDIATE: 0,
    PaymentTerms.NET_7: 7,
    PaymentTerms.NET_14: 14,
    PaymentTerms.NET_30: 30,
    PaymentTerms.NET_60: 60,
    PaymentTerms.NET_90: 90,
}


class RfqStatus(models.TextChoices):
    """Lifecycle of a request for quotation (a sourcing overlay — no GL effect).

    DRAFT     -> being prepared; lines editable.
    ISSUED    -> sent to vendors; quotations may be submitted against it.
    AWARDED   -> a vendor's quotation was selected and converted to a PO.
    CLOSED    -> finished with no award (e.g. all quotes rejected / cancelled sourcing).
    CANCELLED -> abandoned before issue or award.
    """
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    AWARDED = "AWARDED", "Awarded"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


class QuotationStatus(models.TextChoices):
    """Lifecycle of a vendor's quotation against an RFQ.

    DRAFT     -> being captured.
    SUBMITTED -> a firm offer in contention.
    AWARDED   -> selected; converted into a purchase order.
    REJECTED  -> not selected (set on the losers when a sibling is awarded).
    EXPIRED   -> past its validity date without award.
    """
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    AWARDED = "AWARDED", "Awarded"
    REJECTED = "REJECTED", "Rejected"
    EXPIRED = "EXPIRED", "Expired"


class ContractStatus(models.TextChoices):
    """Lifecycle of a vendor contract (a master-data overlay — no GL effect).

    DRAFT      -> being prepared; not yet in force.
    ACTIVE     -> in force between start_date and end_date.
    EXPIRED    -> past end_date without renewal/termination.
    TERMINATED -> ended early.
    RENEWED    -> superseded by a successor contract (its renewal).
    """
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    EXPIRED = "EXPIRED", "Expired"
    TERMINATED = "TERMINATED", "Terminated"
    RENEWED = "RENEWED", "Renewed"


class MilestoneStatus(models.TextChoices):
    """Delivery state of a contract milestone.

    PENDING   -> not yet delivered.
    COMPLETED -> delivered / met.
    MISSED    -> due date passed without completion.
    """
    PENDING = "PENDING", "Pending"
    COMPLETED = "COMPLETED", "Completed"
    MISSED = "MISSED", "Missed"


class ProcApprovalState(models.TextChoices):
    """Spend-approval state of a procurement document, driven by ``vs_workflow``.

    A governance overlay *independent of* the ledger ``status`` (DRAFT → POSTED …):
    it records where the document sits in its approval workflow without colliding
    with the posting lifecycle (a vendor invoice is still posted from DRAFT).

    NOT_SUBMITTED -> never sent for approval (or withdrawn/cancelled back to start).
    PENDING       -> a workflow instance is in flight.
    APPROVED      -> the workflow fully approved it.
    REJECTED      -> the workflow terminally rejected it.
    """
    NOT_SUBMITTED = "NOT_SUBMITTED", "Not submitted"
    PENDING = "PENDING", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


#: ``document_type`` tokens registered with ``vs_workflow`` (see workflow_handlers.py).
WF_DOCTYPE_REQUISITION = "procurement.requisition"
WF_DOCTYPE_PURCHASE_ORDER = "procurement.purchase_order"
WF_DOCTYPE_VENDOR_INVOICE = "procurement.vendor_invoice"
WF_DOCTYPE_VENDOR_PAYMENT = "procurement.vendor_payment"

# One canonical boundary for every shared-workflow record owned by Procurement.
# Keep queue/report adapters on this allow-list so adding a new approvable document
# cannot silently expose unrelated workflows or disappear from one Procurement view.
PROCUREMENT_APPROVAL_TYPES = (
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_VENDOR_INVOICE,
    WF_DOCTYPE_VENDOR_PAYMENT,
)

#: Template code the default-template provisioner publishes and submission resolves to.
WF_DEFAULT_TEMPLATE_CODE = "standard"

#: Default amount (kobo) at/above which the second (senior) approval stage is included.
#: ₦500,000.00 — overridable per call to ``ensure_default_approval_templates``.
WF_DEFAULT_SENIOR_THRESHOLD = 50_000_000

#: Default RBAC permission keys the seeded approval stages resolve approvers against.
WF_DEFAULT_MANAGER_PERMISSION = "procurement.approval.approve"
WF_DEFAULT_SENIOR_PERMISSION = "procurement.approval.approve_senior"


class MatchStatus(models.TextChoices):
    """Outcome of the 3-way match (PO ↔ GRN ↔ vendor invoice).

    NOT_MATCHED    -> not yet run.
    AUTO_MATCHED   -> quantities and prices agree within tolerance; safe to post.
    UNDER_RECEIVED -> billed for more than has been received (GRN short) — blocked.
    OVER_BILLED    -> billed beyond the PO quantity/price — blocked.
    PRICE_VARIANCE -> received OK but unit price differs from the PO — flag/approve.
    """
    NOT_MATCHED = "NOT_MATCHED", "Not matched"
    AUTO_MATCHED = "AUTO_MATCHED", "Auto-matched"
    UNDER_RECEIVED = "UNDER_RECEIVED", "Under received"
    OVER_BILLED = "OVER_BILLED", "Over billed"
    PRICE_VARIANCE = "PRICE_VARIANCE", "Price variance"


#: Match outcomes that must NOT post without an explicit variance override.
MATCH_BLOCKING = frozenset({MatchStatus.UNDER_RECEIVED, MatchStatus.OVER_BILLED})

class StockMovementType(models.TextChoices):
    """Direction/reason of a stock-ledger entry (perpetual inventory).

    RECEIPT    -> goods came in (raised by a stock-tracked GRN line, or manually).
                  Increases on-hand qty and value; sets the moving-average cost.
    ISSUE      -> goods consumed/dispatched. Decreases on-hand at the current
                  moving-average cost (Dr expense, Cr inventory).
    ADJUSTMENT -> a stock-count / write-down / write-up correction (signed). Posts
                  the value delta between inventory and an adjustment account.
    """
    RECEIPT = "RECEIPT", "Receipt"
    ISSUE = "ISSUE", "Issue"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"


#: Well-known Chart-of-Accounts codes the P2P journals resolve against (per entity).
GRIR_CLEARING_CODE = "2150"   # Goods-Received / Invoice-Received clearing (liability)
WHT_PAYABLE_CODE = "2300"     # Withholding-tax payable (liability)
INVENTORY_ASSET_CODE = "1400"        # Inventory / stock on hand (asset)
INVENTORY_ADJUSTMENT_CODE = "5150"   # Inventory adjustments / shrinkage (expense)
