"""Procurement models (vs_procurement) — Phase 3, Procure-to-Pay.

The purchasing side of the ledger and the Accounts-Payable sub-ledger. **Procurement
depends on finance, never the reverse:** every document is scoped to a
:class:`vs_finance.models.LedgerEntity` (the tenant — never a School), money is integer
kobo via :class:`vs_finance.money.MoneyField`, and the posting documents (GRN, vendor
invoice, vendor payment) raise journals through the finance posting service so the same
period-lock and balance guards apply.

The chain modelled here:

    PurchaseRequisition → PurchaseOrder → GoodsReceivedNote → VendorInvoice → VendorPayment

and the AP sub-ledger (:class:`Vendor` + :class:`VendorInvoice` + :class:`VendorPayment`)
that mirrors the AR sub-ledger in :mod:`vs_finance.models`. The classic three-document
control — **GR/IR clearing** — sits between receipt and invoice: receiving debits the
expense and credits GR/IR; the matched invoice debits GR/IR (clearing it) and credits
AP. When goods are both received and billed, GR/IR nets to zero.

The sourcing overlay (RFQ → VendorQuotation → award), the item :class:`CatalogItem` and
:class:`VendorContract` (with :class:`ContractMilestone`) sit off the journal-posting
path and add no GL behaviour — they feed the same chain. The full double-entry P2P chain
is here.
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from vs_finance.constants import DocType, InvoicePaymentStatus, PaymentMethod
from vs_finance.models import FinanceDocument, TimeStampedModel
from vs_finance.money import MoneyField

from .constants import (
    ContractStatus,
    MatchStatus,
    MilestoneStatus,
    PaymentTerms,
    ProcApprovalState,
    QuotationStatus,
    RfqStatus,
    StockMovementType,
    VendorKycStatus,
    VendorRisk,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
    WF_DOCTYPE_VENDOR_PAYMENT,
)


def _pct(part, whole) -> Decimal:
    """Percentage ``part/whole`` as a 2dp Decimal; 0 when ``whole`` is 0."""
    whole = Decimal(whole or 0)
    if whole == 0:
        return Decimal("0.00")
    return (Decimal(part or 0) / whole * 100).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
# Master data — vendors                                                       #
# --------------------------------------------------------------------------- #

class VendorCategory(TimeStampedModel):
    """A grouping of vendors with a default expense account (e.g. 'Utilities').

    The ``default_expense_account`` seeds new purchase lines so buyers don't pick a GL
    account by hand each time; it's only a default and can be overridden per line.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT,
        related_name="vendor_categories",
    )
    code = models.CharField(max_length=32, help_text="Unique within the entity.")
    name = models.CharField(max_length=160)
    default_expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="vendor_categories", null=True, blank=True,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_proc_vendorcat_entity_code",
            ),
        ]
        ordering = ["entity", "code"]
        verbose_name_plural = "vendor categories"

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class Vendor(TimeStampedModel):
    """A payable party — the AP sub-ledger account — for one entity.

    The mirror image of :class:`vs_finance.models.Customer`: ``payable_account`` is the
    AP control account this vendor's balance rolls into, and the optional
    ``source_type``/``source_id`` pair is a *loose* string reference to an originating
    domain record (never an FK), keeping the ledger decoupled from product apps.

    ``kyc_status``/``on_hold`` are payment gates the payables service checks before
    cutting a cheque; ``default_wht_tax_code`` drives withholding-tax on payment.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="vendors",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="vendors", null=True, blank=True,
    )
    code = models.CharField(max_length=32, help_text="Vendor code, unique within the entity.")
    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        VendorCategory, on_delete=models.PROTECT, related_name="vendors",
        null=True, blank=True,
    )

    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    address = models.TextField(blank=True, default="")

    tax_id = models.CharField(max_length=32, blank=True, default="", help_text="TIN / tax identifier.")
    tax_id_normalized = models.CharField(
        max_length=32, blank=True, default="", editable=False,
        help_text="Canonical tax identifier used only for entity-scoped duplicate detection.",
    )
    bank_name = models.CharField(max_length=120, blank=True, default="")
    bank_account_number = models.CharField(max_length=32, blank=True, default="")
    bank_account_name = models.CharField(max_length=160, blank=True, default="")

    payable_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="ap_vendors",
        null=True, blank=True,
        help_text="AP control account this vendor's balance rolls into.",
    )
    default_expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="default_vendors",
        null=True, blank=True,
    )
    default_wht_tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT, related_name="wht_vendors",
        null=True, blank=True,
        help_text="Withholding-tax code applied to this vendor's payments.",
    )

    payment_terms = models.CharField(
        max_length=8, choices=PaymentTerms.choices, default=PaymentTerms.NET_30,
    )
    kyc_status = models.CharField(
        max_length=8, choices=VendorKycStatus.choices, default=VendorKycStatus.PENDING,
    )
    risk = models.CharField(max_length=6, choices=VendorRisk.choices, default=VendorRisk.LOW)
    on_hold = models.BooleanField(default=False, help_text="Block new POs/payments while True.")

    opening_balance = MoneyField(help_text="Opening AP balance in kobo (informational).")
    source_type = models.CharField(max_length=64, blank=True, default="")
    source_id = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_proc_vendor_entity_code",
            ),
            models.UniqueConstraint(
                Lower("code"), "entity", name="uniq_proc_vendor_entity_code_ci",
            ),
            models.UniqueConstraint(
                fields=["entity", "tax_id_normalized"],
                condition=~models.Q(tax_id_normalized=""),
                name="uniq_proc_vendor_entity_tax_id_norm",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["entity", "on_hold"]),
            models.Index(fields=["source_type", "source_id"]),
        ]
        ordering = ["entity", "code"]

    def save(self, *args, **kwargs):
        """Keep duplicate-detection identifiers canonical for every ORM write path."""
        self.code = str(self.code or "").strip().upper()
        self.tax_id = str(self.tax_id or "").strip().upper()
        self.tax_id_normalized = re.sub(r"[^A-Z0-9]", "", self.tax_id)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "tax_id" in update_fields:
            # Callers that intentionally update only tax_id must persist its paired key too.
            kwargs["update_fields"] = set(update_fields) | {"tax_id_normalized"}
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


# --------------------------------------------------------------------------- #
# Master data — item catalog                                                  #
# --------------------------------------------------------------------------- #

class CatalogItem(TimeStampedModel):
    """A reusable purchasable item — pre-set buying defaults so lines aren't retyped.

    Pure master data with **no GL effect**: a catalog item names a good/service and
    carries the defaults a buyer would otherwise pick by hand on every requisition / RFQ
    / PO line — a ``preferred_vendor``, the GL ``default_expense_account`` the cost lands
    in, a ``default_tax_code``, an indicative ``standard_unit_price`` (kobo) and a
    ``lead_time_days`` planning hint. :meth:`line_defaults` returns those as a dict the
    line-building views can splat in. None of it is binding — every value is overridable
    per line.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="catalog_items",
    )
    code = models.CharField(max_length=40, help_text="Item code, unique within the entity.")
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=255, blank=True, default="")
    unit_of_measure = models.CharField(
        max_length=24, blank=True, default="each", help_text="e.g. 'each', 'box', 'hour'.",
    )

    preferred_vendor = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL, related_name="catalog_items",
        null=True, blank=True,
    )
    default_expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="catalog_items",
        null=True, blank=True,
        help_text="GL account the cost lands in (seeds purchase lines).",
    )
    default_tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT, related_name="catalog_items",
        null=True, blank=True,
    )
    lead_time_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Typical delivery lead time, in days.",
    )
    standard_unit_price = MoneyField(help_text="Indicative price per unit, in kobo.")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_proc_catalogitem_entity_code",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["preferred_vendor"]),
        ]
        ordering = ["entity", "code"]

    def line_defaults(self) -> dict:
        """The buying defaults to seed a requisition / RFQ / PO line from this item."""
        return {
            "description": self.description or self.name,
            "expense_account": self.default_expense_account,
            "tax_code": self.default_tax_code,
            "unit_price": self.standard_unit_price,
        }

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


# --------------------------------------------------------------------------- #
# Inventory / stock ledger (perpetual, weighted-average cost)                 #
# --------------------------------------------------------------------------- #

class StockItem(TimeStampedModel):
    """A physically stocked good — carries live on-hand quantity and its GL value.

    Distinct from :class:`CatalogItem`: a catalog item is *buying* master data (defaults
    that pre-fill purchase lines, including services you never hold), whereas a stock item
    is *inventory* state — what is physically held, counted, and carried on the balance
    sheet. The optional :attr:`catalog_item` link joins a stocked good to its buying
    defaults when one exists.

    Valuation is **weighted-average** held without floats: rather than storing a
    fractional unit cost, the item carries integer ``on_hand_qty`` and the total
    ``stock_value`` (kobo); the moving-average unit cost is *derived* (:attr:`unit_cost`).
    Each :class:`StockMovement` adjusts both atomically, so ``stock_value`` always equals
    the perpetual-inventory balance for this item in :attr:`inventory_account`.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="stock_items",
    )
    code = models.CharField(max_length=40, help_text="Stock code, unique within the entity.")
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=255, blank=True, default="")
    unit_of_measure = models.CharField(max_length=24, blank=True, default="each")

    catalog_item = models.ForeignKey(
        CatalogItem, on_delete=models.SET_NULL, related_name="stock_items",
        null=True, blank=True,
        help_text="Optional link to the buying-defaults catalog entry for this good.",
    )
    inventory_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="stock_items",
        help_text="Balance-sheet asset account this item's value is carried in.",
    )
    default_expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="stock_items_expense",
        null=True, blank=True,
        help_text="Default account debited when stock is issued (e.g. Cost of Sales).",
    )

    reorder_level = models.DecimalField(
        max_digits=14, decimal_places=4, default=0,
        help_text="On-hand at/below which the item is flagged for reorder.",
    )
    reorder_qty = models.DecimalField(
        max_digits=14, decimal_places=4, default=0,
        help_text="Suggested quantity to reorder when low.",
    )

    on_hand_qty = models.DecimalField(
        max_digits=16, decimal_places=4, default=0,
        help_text="Live quantity on hand (maintained by the stock ledger).",
    )
    stock_value = MoneyField(
        help_text="Total value of on-hand stock, in kobo (weighted-average basis).",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_proc_stockitem_entity_code",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["catalog_item"]),
        ]
        ordering = ["entity", "code"]

    @property
    def unit_cost(self) -> int:
        """Derived weighted-average unit cost in kobo (0 when nothing on hand)."""
        if self.on_hand_qty and self.on_hand_qty > 0:
            # Weighted-average unit cost divides the perpetual stock value by the live quantity on hand.
            return int((Decimal(self.stock_value) / Decimal(self.on_hand_qty)).to_integral_value())
        return 0

    @property
    def needs_reorder(self) -> bool:
        return self.on_hand_qty <= self.reorder_level

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class StockMovement(TimeStampedModel):
    """One immutable line of the perpetual stock ledger for a :class:`StockItem`.

    Signed in both quantity and value (``+`` in, ``-`` out) so a running sum reproduces
    the on-hand balance. ``balance_qty`` / ``balance_value`` snapshot the item's state
    *after* this movement for audit and aged-stock reporting, and ``journal`` links the
    GL entry the movement posted (a stock-tracked GRN line, an issue, or an adjustment).
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="stock_movements",
    )
    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="movements",
    )
    movement_type = models.CharField(max_length=16, choices=StockMovementType.choices)
    movement_date = models.DateField()

    quantity = models.DecimalField(
        max_digits=16, decimal_places=4,
        help_text="Signed quantity change (+ receipt, − issue).",
    )
    value_amount = models.BigIntegerField(
        help_text="Signed value change in kobo (+ in, − out).",
    )
    balance_qty = models.DecimalField(
        max_digits=16, decimal_places=4, default=0,
        help_text="On-hand quantity after this movement.",
    )
    balance_value = MoneyField(help_text="Stock value (kobo) after this movement.")

    grn = models.ForeignKey(
        "GoodsReceivedNote", on_delete=models.SET_NULL, related_name="stock_movements",
        null=True, blank=True,
    )
    journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT, related_name="stock_movements",
        null=True, blank=True,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="stock_movements", null=True, blank=True,
    )

    class Meta:
        ordering = ["-movement_date", "-id"]
        indexes = [
            models.Index(fields=["entity", "movement_date"]),
            models.Index(fields=["stock_item", "movement_date"]),
            models.Index(fields=["movement_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.movement_type} {self.quantity} of {self.stock_item_id}"


# --------------------------------------------------------------------------- #
# Vendor contracts (master data — no GL effect)                               #
# --------------------------------------------------------------------------- #

class VendorContract(TimeStampedModel):
    """A term agreement with a vendor — the basis for renewal/expiry alerts.

    Pure master data with **no GL effect**: a contract records the commercial envelope
    (period, value, payment terms) and an optional list of :class:`ContractMilestone` s.
    ``status`` runs its own lifecycle (:class:`~vs_procurement.constants.ContractStatus`).
    A contract whose ``end_date`` is within ``renewal_notice_days`` of a given date is
    surfaced as *due for renewal* by :func:`vs_procurement.contracts.expiring_contracts`;
    ``renews`` points a successor contract back at the one it replaced.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="vendor_contracts",
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="contracts")
    reference = models.CharField(max_length=64, help_text="Contract reference, unique within the entity.")
    title = models.CharField(max_length=200)
    status = models.CharField(
        max_length=10, choices=ContractStatus.choices, default=ContractStatus.DRAFT,
    )

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    contract_value = MoneyField(help_text="Total committed value over the term, in kobo.")
    payment_terms = models.CharField(
        max_length=8, choices=PaymentTerms.choices, default=PaymentTerms.NET_30,
    )

    auto_renew = models.BooleanField(default=False)
    renewal_notice_days = models.PositiveSmallIntegerField(
        default=30, help_text="Days before end_date to flag the contract for renewal.",
    )
    renews = models.ForeignKey(
        "self", on_delete=models.SET_NULL, related_name="renewed_by",
        null=True, blank=True, help_text="The prior contract this one renews/replaces.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="vendor_contracts", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "reference"], name="uniq_proc_contract_entity_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "end_date"]),
            models.Index(fields=["vendor"]),
        ]
        ordering = ["entity", "-end_date", "reference"]

    def renewal_window_start(self):
        """The date from which this contract starts appearing in renewal alerts."""
        if self.end_date is None:
            return None
        return self.end_date - datetime.timedelta(days=self.renewal_notice_days)

    def __str__(self) -> str:
        return f"{self.reference} · {self.title}"


class ContractMilestone(TimeStampedModel):
    """A deliverable / payment checkpoint on a :class:`VendorContract`."""

    contract = models.ForeignKey(
        VendorContract, on_delete=models.CASCADE, related_name="milestones",
    )
    name = models.CharField(max_length=200)
    due_date = models.DateField(null=True, blank=True)
    amount = MoneyField(help_text="Value tied to this milestone, in kobo.")
    status = models.CharField(
        max_length=10, choices=MilestoneStatus.choices, default=MilestoneStatus.PENDING,
    )
    completed_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["contract", "line_no", "due_date", "id"]
        indexes = [
            models.Index(fields=["contract"]),
            models.Index(fields=["status", "due_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"


# --------------------------------------------------------------------------- #
# Purchase requisition (intent to buy — no GL effect)                         #
# --------------------------------------------------------------------------- #

class PurchaseRequisition(FinanceDocument):
    """An internal request to buy — the start of the procurement chain.

    No GL effect: a requisition is intent, approved (via ``vs_workflow``) and then
    converted into one or more :class:`PurchaseOrder` s. ``status`` uses the shared
    document lifecycle (DRAFT → PENDING_APPROVAL → APPROVED → CANCELLED).
    """

    DOC_TYPE = DocType.PURCHASE_REQUISITION
    #: vs_workflow integration — see vs_procurement.workflow_handlers / .approvals.
    workflow_document_type = WF_DOCTYPE_REQUISITION
    workflow_amount_field = "estimated_total"

    title = models.CharField(max_length=200, blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="purchase_requisitions", null=True, blank=True,
    )
    request_date = models.DateField()
    needed_by = models.DateField(null=True, blank=True)
    cost_center = models.ForeignKey(
        "vs_finance.CostCenter", on_delete=models.PROTECT,
        related_name="purchase_requisitions", null=True, blank=True,
    )
    justification = models.CharField(max_length=255, blank=True, default="")
    estimated_total = MoneyField(help_text="Rolled-up estimate from the lines, in kobo.")
    approval_state = models.CharField(
        max_length=16, choices=ProcApprovalState.choices,
        default=ProcApprovalState.NOT_SUBMITTED,
        help_text="Spend-approval state driven by vs_workflow (overlay; not the ledger status).",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "request_date"]),
        ]

    def recompute_total(self, *, save: bool = True) -> None:
        # Requisition value is the sum of each estimated line value; it has no tax posting at this intent stage.
        total = sum((ln.estimated_line_total for ln in self.lines.all()), 0)
        self.estimated_total = total
        if save:
            self.save(update_fields=["estimated_total", "updated_at"])


class PurchaseRequisitionLine(TimeStampedModel):
    """One requested item on a :class:`PurchaseRequisition` (estimate only)."""

    requisition = models.ForeignKey(
        PurchaseRequisition, on_delete=models.CASCADE, related_name="lines",
    )
    catalog_item = models.ForeignKey(
        CatalogItem, on_delete=models.PROTECT, related_name="requisition_lines",
        null=True, blank=True,
    )
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit = models.CharField(max_length=24, blank=True, default="Unit")
    estimated_unit_price = MoneyField(help_text="Estimated price per unit, in kobo.")
    expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="requisition_lines", null=True, blank=True,
    )
    tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT,
        related_name="requisition_lines", null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["requisition", "line_no", "id"]
        indexes = [models.Index(fields=["requisition"])]

    @property
    def estimated_line_total(self) -> int:
        # Money is stored as integer kobo, so round the quantity extension to the nearest whole kobo.
        return int((Decimal(self.quantity) * Decimal(self.estimated_unit_price)).to_integral_value())

    def __str__(self) -> str:
        return f"{self.description}: {self.quantity}"


# --------------------------------------------------------------------------- #
# Sourcing — RFQ → vendor quotations → award (no GL effect)                   #
# --------------------------------------------------------------------------- #

class RequestForQuotation(FinanceDocument):
    """A request inviting vendors to quote — competitive sourcing before a PO.

    A sourcing overlay with no GL effect: an RFQ (optionally raised off an approved
    :class:`PurchaseRequisition`) is issued to vendors who reply with
    :class:`VendorQuotation` s; awarding the winning quote converts it into a
    :class:`PurchaseOrder`. ``rfq_status`` runs its own lifecycle
    (:class:`~vs_procurement.constants.RfqStatus`); the inherited ``status`` is unused.
    """

    DOC_TYPE = DocType.RFQ

    requisition = models.ForeignKey(
        PurchaseRequisition, on_delete=models.PROTECT, related_name="rfqs",
        null=True, blank=True,
    )
    title = models.CharField(max_length=200, blank=True, default="")
    rfq_status = models.CharField(
        max_length=10, choices=RfqStatus.choices, default=RfqStatus.DRAFT,
    )
    issue_date = models.DateField()
    response_due_date = models.DateField(
        null=True, blank=True, help_text="Closing date for vendor responses.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "rfq_status"]),
            models.Index(fields=["entity", "issue_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.document_number or 'RFQ?'} · {self.title}"


class RfqLine(TimeStampedModel):
    """One requested item on an :class:`RequestForQuotation` (specification only)."""

    rfq = models.ForeignKey(RequestForQuotation, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    requisition_line = models.ForeignKey(
        PurchaseRequisitionLine, on_delete=models.PROTECT,
        related_name="rfq_lines", null=True, blank=True,
    )
    expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT,
        related_name="rfq_lines", null=True, blank=True,
    )
    tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT,
        related_name="rfq_lines", null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["rfq", "line_no", "id"]
        indexes = [models.Index(fields=["rfq"])]

    def __str__(self) -> str:
        return f"{self.description}: {self.quantity}"


class VendorQuotation(FinanceDocument):
    """A vendor's priced offer against an :class:`RequestForQuotation`.

    No GL effect. ``quotation_status`` runs its own lifecycle
    (:class:`~vs_procurement.constants.QuotationStatus`); the inherited ``status`` is
    unused. Awarding the quote (:func:`vs_procurement.sourcing.award_quotation`) builds a
    DRAFT :class:`PurchaseOrder` from the quotation's lines.
    """

    DOC_TYPE = DocType.QUOTATION

    rfq = models.ForeignKey(
        RequestForQuotation, on_delete=models.PROTECT, related_name="quotations",
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="quotations")
    quotation_status = models.CharField(
        max_length=10, choices=QuotationStatus.choices, default=QuotationStatus.DRAFT,
    )
    quote_date = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT, related_name="quotations",
        null=True, blank=True,
    )
    lead_time_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Promised delivery lead time in days.",
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    notes = models.CharField(max_length=255, blank=True, default="")

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")

    awarded_po = models.ForeignKey(
        "PurchaseOrder", on_delete=models.SET_NULL, related_name="source_quotation",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "quotation_status"]),
            models.Index(fields=["rfq"]),
            models.Index(fields=["vendor"]),
        ]

    def recompute_totals(self, *, save: bool = True) -> None:
        # Quotation gross is the sum of line net values plus their calculated tax.
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def __str__(self) -> str:
        return f"{self.document_number or 'QUO?'} · {self.vendor.code}"


class VendorQuotationLine(TimeStampedModel):
    """One priced line of a :class:`VendorQuotation`."""

    quotation = models.ForeignKey(
        VendorQuotation, on_delete=models.CASCADE, related_name="lines",
    )
    rfq_line = models.ForeignKey(
        RfqLine, on_delete=models.PROTECT, related_name="quotation_lines",
        null=True, blank=True,
    )
    description = models.CharField(max_length=255)
    expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="quotation_lines",
        null=True, blank=True,
        help_text="GL account the cost lands in once received (carried onto the PO line).",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Quoted price per unit, in kobo.")
    tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT, related_name="quotation_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["quotation", "line_no", "id"]
        indexes = [models.Index(fields=["quotation"])]

    def __str__(self) -> str:
        return f"{self.description}: {self.quantity} @ {self.unit_price}"


# --------------------------------------------------------------------------- #
# Purchase order (commitment — no GL effect until receipt)                    #
# --------------------------------------------------------------------------- #

class PurchaseOrder(FinanceDocument):
    """A commitment to buy from a :class:`Vendor` at agreed prices.

    Still no GL posting (a commitment, not an expense); the cost hits the ledger when
    goods are received. Lines track ``received_qty``/``invoiced_qty`` so the PO knows
    how far through fulfilment and billing it is (``received_pct``/``invoiced_pct``).
    """

    DOC_TYPE = DocType.PURCHASE_ORDER
    #: vs_workflow integration — see vs_procurement.workflow_handlers / .approvals.
    workflow_document_type = WF_DOCTYPE_PURCHASE_ORDER
    workflow_amount_field = "total"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    requisition = models.ForeignKey(
        PurchaseRequisition, on_delete=models.PROTECT, related_name="purchase_orders",
        null=True, blank=True,
    )
    order_date = models.DateField()
    expected_date = models.DateField(null=True, blank=True)
    delivery_address = models.TextField(blank=True, default="")
    payment_terms = models.CharField(max_length=128, blank=True, default="")
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT, related_name="purchase_orders",
        null=True, blank=True,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    approval_state = models.CharField(
        max_length=16, choices=ProcApprovalState.choices,
        default=ProcApprovalState.NOT_SUBMITTED,
        help_text="Spend-approval state driven by vs_workflow (overlay; not the ledger status).",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["vendor"]),
            models.Index(fields=["entity", "order_date"]),
        ]

    def recompute_totals(self, *, save: bool = True) -> None:
        # PO gross commitment is the sum of line net values plus their calculated tax.
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    @property
    def received_pct(self) -> Decimal:
        # Fulfilment percentage compares aggregate received quantity with aggregate ordered quantity.
        ordered = sum((Decimal(l.quantity) for l in self.lines.all()), Decimal(0))
        received = sum((Decimal(l.received_qty) for l in self.lines.all()), Decimal(0))
        return _pct(received, ordered)

    @property
    def invoiced_pct(self) -> Decimal:
        # Billing percentage compares aggregate invoiced quantity with aggregate ordered quantity.
        ordered = sum((Decimal(l.quantity) for l in self.lines.all()), Decimal(0))
        invoiced = sum((Decimal(l.invoiced_qty) for l in self.lines.all()), Decimal(0))
        return _pct(invoiced, ordered)

    @property
    def is_fully_received(self) -> bool:
        # Every line must meet its own ordered quantity; aggregate equality could hide an over-received line.
        return all(Decimal(l.received_qty) >= Decimal(l.quantity) for l in self.lines.all())


class PurchaseOrderLine(TimeStampedModel):
    """One ordered item on a :class:`PurchaseOrder`, mapped to a GL expense account.

    ``received_qty`` and ``invoiced_qty`` are advanced by goods receipts and vendor
    invoices respectively; the three-way match compares them against ``quantity``.
    """

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines",
    )
    requisition_line = models.ForeignKey(
        PurchaseRequisitionLine, on_delete=models.PROTECT,
        related_name="po_lines", null=True, blank=True,
    )
    description = models.CharField(max_length=255)
    expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="po_lines",
        help_text="GL account the cost lands in when goods are received.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Agreed price per unit, in kobo.")
    tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT, related_name="po_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    received_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    invoiced_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    cost_center = models.ForeignKey(
        "vs_finance.CostCenter", on_delete=models.PROTECT, related_name="po_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["purchase_order", "line_no", "id"]
        indexes = [
            models.Index(fields=["purchase_order"]),
            models.Index(fields=["expense_account"]),
        ]

    @property
    def outstanding_qty(self) -> Decimal:
        return Decimal(self.quantity) - Decimal(self.received_qty)

    @property
    def received_pct(self) -> Decimal:
        return _pct(self.received_qty, self.quantity)

    @property
    def invoiced_pct(self) -> Decimal:
        return _pct(self.invoiced_qty, self.quantity)

    def __str__(self) -> str:
        return f"{self.description}: {self.quantity} @ {self.unit_price}"


# --------------------------------------------------------------------------- #
# Goods received note (posts Dr expense, Cr GR/IR)                            #
# --------------------------------------------------------------------------- #

class GoodsReceivedNote(FinanceDocument):
    """A record that goods/services arrived — the first GL event in the chain.

    Posting (:func:`vs_procurement.purchasing.post_grn`) debits the expense/inventory
    account and credits **GR/IR clearing** for the accepted value (ex-tax): the cost is
    recognised on receipt, while the matching liability waits in GR/IR for the vendor's
    invoice. ``journal`` links the entry raised; ``status`` goes DRAFT → POSTED.
    """

    DOC_TYPE = DocType.GOODS_RECEIVED

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="goods_receipts")
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="goods_receipts",
        null=True, blank=True,
    )
    received_date = models.DateField()
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="goods_receipts", null=True, blank=True,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    total_value = MoneyField(help_text="Accepted value (ex-tax), in kobo.")
    journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT, related_name="grns",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["vendor"]),
            models.Index(fields=["entity", "received_date"]),
        ]

    def recompute_total(self, *, save: bool = True) -> None:
        # Receipt value is the sum of accepted line extensions; rejected quantities never enter the GL value.
        agg = self.lines.aggregate(v=models.Sum("value_amount"))
        self.total_value = agg["v"] or 0
        if save:
            self.save(update_fields=["total_value", "updated_at"])


class GoodsReceivedNoteLine(TimeStampedModel):
    """One received item: accepted/rejected quantities and the value booked.

    ``value_amount`` (kobo) = ``accepted_qty × unit_price`` is what posts on receipt.
    For a non-stock line the debit lands in ``expense_account`` (Dr expense, Cr GR/IR).
    When ``stock_item`` is set the line is **perpetual inventory**: the debit is redirected
    to the item's ``inventory_account`` and a receipt :class:`StockMovement` raises the
    on-hand quantity/value at this cost. Rejected quantity is recorded for the
    returns/quality trail but does not post.
    """

    grn = models.ForeignKey(
        GoodsReceivedNote, on_delete=models.CASCADE, related_name="lines",
    )
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, related_name="grn_lines",
        null=True, blank=True,
    )
    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="grn_lines",
        null=True, blank=True,
        help_text="If set, the receipt is capitalised to inventory (perpetual stock).",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="grn_lines",
        help_text="GL account debited for the accepted value (non-stock lines).",
    )
    accepted_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    rejected_qty = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expected_qty = models.DecimalField(
        max_digits=14, decimal_places=4, default=0,
        help_text="PO quantity remaining when this receipt was created.",
    )
    unit_price = MoneyField(help_text="Price per unit, in kobo (from the PO).")
    value_amount = MoneyField(help_text="accepted_qty × unit_price, in kobo.")
    cost_center = models.ForeignKey(
        "vs_finance.CostCenter", on_delete=models.PROTECT, related_name="grn_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["grn", "line_no", "id"]
        indexes = [models.Index(fields=["grn"]), models.Index(fields=["po_line"])]

    def __str__(self) -> str:
        return f"{self.description or self.expense_account_id}: {self.accepted_qty}"


# --------------------------------------------------------------------------- #
# Vendor invoice (posts Dr GR/IR + input VAT, Cr AP)                          #
# --------------------------------------------------------------------------- #

class VendorInvoice(FinanceDocument):
    """A bill from a :class:`Vendor` — the AP-side mirror of a sales invoice.

    Posting (:func:`vs_procurement.payables.post_vendor_invoice`) runs the three-way
    match, then raises the AP journal: **Dr GR/IR clearing** (clearing what receipt
    parked there) **+ Dr input VAT** (recoverable), **Cr AP control** (the gross owed).
    A non-PO bill debits the expense account directly instead of GR/IR. ``match_status``
    captures the match outcome; ``payment_status`` tracks cash settled, like AR.
    """

    DOC_TYPE = DocType.VENDOR_INVOICE
    #: vs_workflow integration — see vs_procurement.workflow_handlers / .approvals.
    workflow_document_type = WF_DOCTYPE_VENDOR_INVOICE
    workflow_amount_field = "total"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="invoices")
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="vendor_invoices",
        null=True, blank=True,
    )
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT, related_name="vendor_invoices",
        null=True, blank=True,
    )
    vendor_reference = models.CharField(
        max_length=64, blank=True, default="", help_text="The vendor's own invoice number.",
    )
    narration = models.CharField(max_length=255, blank=True, default="")

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    amount_paid = MoneyField(help_text="Cash allocated to this bill, in kobo.")
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )
    match_status = models.CharField(
        max_length=16, choices=MatchStatus.choices, default=MatchStatus.NOT_MATCHED,
    )
    approval_state = models.CharField(
        max_length=16, choices=ProcApprovalState.choices,
        default=ProcApprovalState.NOT_SUBMITTED,
        help_text="Spend-approval state driven by vs_workflow (overlay; not the ledger status).",
    )
    journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT, related_name="ap_invoices",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "payment_status"]),
            models.Index(fields=["vendor"]),
            models.Index(fields=["entity", "invoice_date"]),
        ]

    @property
    def balance_due(self) -> int:
        # Outstanding AP is invoice gross less all payment allocations recorded against it.
        return self.total - self.amount_paid

    def recompute_totals(self, *, save: bool = True) -> None:
        # Invoice gross payable is the sum of line net values plus their calculated tax.
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        # Payment status is derived from allocated cash versus gross invoice value, including overpayment as paid.
        if self.amount_paid <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif self.amount_paid >= self.total:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])


class VendorInvoiceLine(TimeStampedModel):
    """One billed line of a :class:`VendorInvoice` → a GL expense account (+ tax).

    Optional ``po_line``/``grn_line`` links let the three-way match line up the bill
    against what was ordered and received.
    """

    vendor_invoice = models.ForeignKey(
        VendorInvoice, on_delete=models.CASCADE, related_name="lines",
    )
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, related_name="vendor_invoice_lines",
        null=True, blank=True,
    )
    grn_line = models.ForeignKey(
        GoodsReceivedNoteLine, on_delete=models.PROTECT, related_name="vendor_invoice_lines",
        null=True, blank=True,
    )
    description = models.CharField(max_length=255, blank=True, default="")
    expense_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="vendor_invoice_lines",
        help_text="GL expense account (used directly for non-PO bills).",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit billed, in kobo.")
    tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT, related_name="vendor_invoice_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        "vs_finance.CostCenter", on_delete=models.PROTECT, related_name="vendor_invoice_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["vendor_invoice", "line_no", "id"]
        indexes = [
            models.Index(fields=["vendor_invoice"]),
            models.Index(fields=["expense_account"]),
        ]

    @property
    def line_total(self) -> int:
        # A vendor-invoice line settles both its net charge and tax component.
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.expense_account_id}: {self.line_total}"


# --------------------------------------------------------------------------- #
# Vendor payment (posts Dr AP, Cr Bank net, Cr WHT)                           #
# --------------------------------------------------------------------------- #

class VendorPayment(FinanceDocument):
    """Money out to a :class:`Vendor`, settling one or more bills — with WHT.

    Posting (:func:`vs_procurement.payables.post_vendor_payment`) debits AP for the
    **gross** settled, credits the bank/cash for the **net** actually paid, and credits
    **WHT payable** for the tax withheld (``gross = net + wht``). The gross is then
    allocated across vendor invoices (the AP-side mirror of receipt allocation).
    """

    DOC_TYPE = DocType.VENDOR_PAYMENT
    #: vs_workflow integration — approval is separate from posting status.
    workflow_document_type = WF_DOCTYPE_VENDOR_PAYMENT
    workflow_amount_field = "gross_amount"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="payments")
    payment_date = models.DateField()
    currency = models.ForeignKey(
        "vs_finance.Currency", on_delete=models.PROTECT, related_name="vendor_payments",
        null=True, blank=True,
    )
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    approval_state = models.CharField(
        max_length=16, choices=ProcApprovalState.choices,
        default=ProcApprovalState.NOT_SUBMITTED,
        help_text="Payment-approval state driven by vs_workflow (overlay; not ledger status).",
    )
    gross_amount = MoneyField(help_text="Total liability settled (Dr AP), in kobo.")
    wht_amount = MoneyField(help_text="Withholding tax retained (Cr WHT payable), in kobo.")
    net_amount = MoneyField(help_text="Cash actually paid out (Cr bank) = gross − WHT, in kobo.")
    allocated_amount = MoneyField(help_text="Gross applied to bills, in kobo.")
    payment_account = models.ForeignKey(
        "vs_finance.Account", on_delete=models.PROTECT, related_name="vendor_payments",
        null=True, blank=True,
        help_text="Bank/cash account credited (where the money left).",
    )
    wht_tax_code = models.ForeignKey(
        "vs_finance.TaxCode", on_delete=models.PROTECT, related_name="vendor_payments_wht",
        null=True, blank=True,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    journal = models.ForeignKey(
        "vs_finance.JournalEntry", on_delete=models.PROTECT, related_name="ap_payments",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "approval_state"], name="proc_pay_entity_approval_idx"),
            models.Index(fields=["vendor"]),
            models.Index(fields=["entity", "payment_date"]),
        ]
        constraints = FinanceDocument.Meta.constraints + [
            models.CheckConstraint(
                check=models.Q(gross_amount__gt=0), name="ck_proc_payment_gross_positive",
            ),
            models.CheckConstraint(
                check=models.Q(wht_amount__gte=0) & models.Q(wht_amount__lte=models.F("gross_amount")),
                name="ck_proc_payment_wht_within_gross",
            ),
            models.CheckConstraint(
                check=models.Q(allocated_amount__gte=0) & models.Q(allocated_amount__lte=models.F("gross_amount")),
                name="ck_proc_payment_alloc_within_gross",
            ),
        ]

    @property
    def unallocated_amount(self) -> int:
        """Gross not yet applied to any bill — an open debit on the vendor."""
        # Unallocated cash remains the gross payment less allocations already attached to invoices.
        return self.gross_amount - self.allocated_amount


class VendorPaymentAllocation(TimeStampedModel):
    """Links a slice of a :class:`VendorPayment` (gross) to a :class:`VendorInvoice`.

    Mirrors :class:`vs_finance.models.PaymentAllocation`: the GL already moved when the
    payment posted (Dr AP, Cr bank/WHT); allocation is the sub-ledger act of saying
    which bills that AP debit settles.
    """

    payment = models.ForeignKey(
        VendorPayment, on_delete=models.CASCADE, related_name="allocations",
    )
    vendor_invoice = models.ForeignKey(
        VendorInvoice, on_delete=models.PROTECT, related_name="allocations",
    )
    amount = MoneyField(help_text="Gross applied to this bill, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "vendor_invoice"],
                name="uniq_proc_alloc_payment_invoice",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_proc_alloc_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["vendor_invoice"]),
            models.Index(fields=["payment"]),
        ]
        ordering = ["payment", "id"]

    def __str__(self) -> str:
        return f"{self.payment_id}→{self.vendor_invoice_id}: {self.amount}"
