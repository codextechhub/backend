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

#: Well-known Chart-of-Accounts codes the P2P journals resolve against (per entity).
GRIR_CLEARING_CODE = "2150"   # Goods-Received / Invoice-Received clearing (liability)
WHT_PAYABLE_CODE = "2300"     # Withholding-tax payable (liability)
