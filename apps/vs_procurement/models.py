"""Procurement models (vs_procurement) - Phase 3, Procure-to-Pay.

The purchasing side of the ledger and the Accounts-Payable sub-ledger. **Procurement
depends on finance, never the reverse:** every document is scoped to a
:class:`vs_finance.models.LedgerEntity` (the tenant - never a School), money is integer
kobo via :class:`vs_finance.money.MoneyField`, and the posting documents (GRN, vendor
invoice, vendor payment) raise journals through the finance posting service so the same
period-lock and balance guards apply.

The chain modelled here:

    PurchaseRequisition → PurchaseOrder → GoodsReceivedNote → VendorInvoice → VendorPayment

and the AP sub-ledger (:class:`Vendor` + :class:`VendorInvoice` + :class:`VendorPayment`)
that mirrors the AR sub-ledger in :mod:`vs_finance.models`. The classic three-document
control - **GR/IR clearing** - sits between receipt and invoice: receiving debits the
expense and credits GR/IR; the matched invoice debits GR/IR (clearing it) and credits
AP. When goods are both received and billed, GR/IR nets to zero.

The sourcing overlay (RFQ → VendorQuotation → award), the item :class:`CatalogItem` and
:class:`VendorContract` (with :class:`ContractMilestone`) sit off the journal-posting
path and add no GL behaviour - they feed the same chain. The full double-entry P2P chain
is here.
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
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
    PurchaseOrderVendorDeliverySource,
    PurchaseOrderVendorDeliveryStatus,
    QuotationLineResponse,
    RfqInvitationStatus,
    VendorPurchaseKycRequirement,
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
    """Return ``part / whole`` as a two-decimal percentage, never a ratio.

    Quantities are :class:`Decimal` values while API percentage fields expect the
    human scale (``50.00`` rather than ``0.50``). A zero denominator is a valid empty
    document state, so it deliberately reports ``0.00`` instead of raising.
    """
    whole = Decimal(whole or 0)
    if whole == 0:
        return Decimal("0.00")
    return (Decimal(part or 0) / whole * 100).quantize(Decimal("0.01"))


class _AutoMasterCodeMixin:
    """Allocate stable tenant-scoped codes for non-semantic master records."""

    AUTO_CODE_PREFIX: str | None = None

    def assign_code(self) -> str:
        self.code = str(self.code or "").strip().upper()
        if self.code:
            return self.code
        if self.entity_id is None:
            raise ValueError(f"{type(self).__name__} needs an entity before code allocation.")
        if not self.AUTO_CODE_PREFIX:
            raise ValueError(f"{type(self).__name__} must define AUTO_CODE_PREFIX.")

        from vs_tenants.numbering import next_tenant_document_number

        self.code = next_tenant_document_number(
            tenant=self.entity.tenant,
            document_code=self.AUTO_CODE_PREFIX,
        )
        return self.code

    def save(self, *args, **kwargs):
        self.code = str(self.code or "").strip().upper()
        if not self.code and self.entity_id:
            with transaction.atomic():
                self.assign_code()
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {"code"}
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Entity settings                                                             #
# --------------------------------------------------------------------------- #

class ProcurementSettings(TimeStampedModel):
    """Typed purchasing defaults and invoice-match policy for one entity."""

    entity = models.OneToOneField(
        "vs_finance.LedgerEntity", on_delete=models.CASCADE,
        related_name="procurement_settings",
    )
    default_payment_terms = models.CharField(
        max_length=8, choices=PaymentTerms.choices, default=PaymentTerms.NET_30,
    )
    default_delivery_address = models.TextField(blank=True, default="")
    quantity_tolerance_bps = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(10000)],
        help_text="Allowed ordered/received quantity overage in basis points.",
    )
    price_tolerance_bps = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(10000)],
        help_text="Allowed unit-price variance in basis points.",
    )
    allow_non_po_invoices = models.BooleanField(
        default=False,
        help_text="Allow bills with no purchase order. Off by default: a non-PO bill "
                  "has no three-way match, so approval is its only control.",
    )
    vendor_purchase_kyc_requirement = models.CharField(
        max_length=20, choices=VendorPurchaseKycRequirement.choices,
        default=VendorPurchaseKycRequirement.PENDING_OR_VERIFIED,
    )
    require_purchase_order_for_receipts = models.BooleanField(default=False)
    default_requisition_lead_days = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(365)],
    )
    contract_renewal_notice_days = models.PositiveSmallIntegerField(
        default=30, validators=[MaxValueValidator(365)],
    )
    default_rfq_response_days = models.PositiveSmallIntegerField(
        default=14, validators=[MaxValueValidator(365)],
        help_text="Default number of days allowed for responses to a new RFQ.",
    )
    rfq_closing_soon_days = models.PositiveSmallIntegerField(
        default=7, validators=[MaxValueValidator(365)],
        help_text="Horizon used by the RFQ closing-soon summary.",
    )
    minimum_rfq_invited_vendors = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)],
        help_text="Minimum distinct vendors required before an RFQ can be issued.",
    )
    minimum_submitted_quotations_before_award = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)],
        help_text="Minimum submitted quotations required before an RFQ can be awarded.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="procurement_settings_updates", null=True, blank=True,
    )

    def __str__(self) -> str:
        return f"Procurement settings for {self.entity_id}"


# --------------------------------------------------------------------------- #
# Master data - vendors                                                       #
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
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children",
        null=True, blank=True,
        help_text="Optional parent category; API governance caps the tree at three levels.",
    )
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
            models.UniqueConstraint(
                Lower("code"), "entity", name="uniq_proc_vendorcat_entity_code_ci",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "parent"], name="proc_vcat_entity_parent_idx"),
        ]
        ordering = ["entity", "code"]
        verbose_name_plural = "vendor categories"

    def save(self, *args, **kwargs):
        """Keep category codes canonical for API and non-API ORM writes."""
        self.code = str(self.code or "").strip().upper()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class Vendor(_AutoMasterCodeMixin, TimeStampedModel):
    """A payable party - the AP sub-ledger account - for one entity.

    The mirror image of :class:`vs_finance.models.Customer`: ``payable_account`` is the
    AP control account this vendor's balance rolls into, and the optional
    ``source_type``/``source_id`` pair is a *loose* string reference to an originating
    domain record (never an FK), keeping the ledger decoupled from product apps.

    ``kyc_status``/``on_hold`` are payment gates the payables service checks before
    cutting a cheque; ``default_wht_tax_code`` drives withholding-tax on payment.
    ``code`` is generated when omitted; trusted imports may preserve an explicit code.
    """

    AUTO_CODE_PREFIX = "VN"

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="vendors",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
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
    bank_code = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Provider bank code used for verified electronic disbursements.",
    )
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
        bank_fields = {
            "bank_name", "bank_code", "bank_account_name", "bank_account_number",
        }
        update_fields = kwargs.get("update_fields")
        bank_fields_to_check = (
            bank_fields if update_fields is None else bank_fields & set(update_fields)
        )
        self.bank_name = str(self.bank_name or "").strip()
        self.bank_code = str(self.bank_code or "").strip().upper()
        self.bank_account_name = str(self.bank_account_name or "").strip()
        self.bank_account_number = re.sub(
            r"\s+", "", str(self.bank_account_number or "").upper(),
        )
        bank_changed = False
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(*bank_fields).first()
            if previous is not None:
                normalizers = {
                    "bank_name": lambda value: str(value or "").strip(),
                    "bank_code": lambda value: str(value or "").strip().upper(),
                    "bank_account_name": lambda value: str(value or "").strip(),
                    "bank_account_number": lambda value: re.sub(
                        r"\s+", "", str(value or "").upper(),
                    ),
                }
                bank_changed = any(
                    normalizers[field](previous[field])
                    != normalizers[field](getattr(self, field))
                    for field in bank_fields_to_check
                )
        self.code = str(self.code or "").strip().upper()
        self.tax_id = str(self.tax_id or "").strip().upper()
        self.tax_id_normalized = re.sub(r"[^A-Z0-9]", "", self.tax_id)
        if bank_changed:
            # A verified decision covers one exact destination. Any master-data change
            # invalidates that decision, including when the same save asks to verify it.
            self.kyc_status = VendorKycStatus.PENDING
        if update_fields is not None and "tax_id" in update_fields:
            # Callers that intentionally update only tax_id must persist its paired key too.
            kwargs["update_fields"] = set(update_fields) | {"tax_id_normalized"}
        if update_fields is not None and bank_changed:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"kyc_status"}
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class VendorContact(TimeStampedModel):
    """A vendor contact with explicit RFQ and purchase-order delivery preferences."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=160, blank=True, default="")
    email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    receives_rfqs = models.BooleanField(default=True)
    receives_purchase_orders = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("email"), "vendor", name="uniq_proc_vendor_contact_email_ci",
            ),
        ]
        indexes = [
            models.Index(fields=["vendor", "is_active", "receives_rfqs"]),
            models.Index(fields=["vendor", "is_active", "receives_purchase_orders"]),
        ]
        ordering = ["vendor", "-is_primary", "name", "id"]

    def __str__(self) -> str:
        return f"{self.name or self.email} · {self.vendor.code}"


# --------------------------------------------------------------------------- #
# Master data - item catalog                                                  #
# --------------------------------------------------------------------------- #

class CatalogItem(_AutoMasterCodeMixin, TimeStampedModel):
    """A reusable purchasable item - pre-set buying defaults so lines aren't retyped.

    Pure master data with **no GL effect**: a catalog item names a good/service and
    carries the defaults a buyer would otherwise pick by hand on every requisition / RFQ
    / PO line - a ``preferred_vendor``, the GL ``default_expense_account`` the cost lands
    in, a ``default_tax_code``, an indicative ``standard_unit_price`` (kobo) and a
    ``lead_time_days`` planning hint. :meth:`line_defaults` returns those as a dict the
    line-building views can splat in. None of it is binding - every value is overridable
    per line.
    """

    AUTO_CODE_PREFIX = "IT"

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="catalog_items",
    )
    code = models.CharField(max_length=40, help_text="Item code, unique within the entity.")
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=255, blank=True, default="")
    unit_of_measure = models.CharField(
        max_length=24, blank=True, default="each", help_text="e.g. 'each', 'box', 'hour'.",
    )

    category = models.ForeignKey(
        VendorCategory, on_delete=models.PROTECT, related_name="catalog_items",
        null=True, blank=True,
        help_text="Optional purchasing taxonomy classification for this item.",
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
            models.UniqueConstraint(
                Lower("code"), "entity", name="uniq_proc_catalogitem_entity_code_ci",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["entity", "category"], name="proc_catalog_ent_cat_idx"),
            models.Index(fields=["preferred_vendor"]),
        ]
        ordering = ["entity", "code"]

    def save(self, *args, **kwargs):
        """Keep the integration-facing item code canonical outside the API too."""
        self.code = str(self.code or "").strip().upper()
        return super().save(*args, **kwargs)

    def line_defaults(self) -> dict:
        """The buying defaults to seed a requisition / RFQ / PO line from this item."""
        expense = self.default_expense_account
        if expense is None and self.category_id and self.category.is_active:
            # Category is a fallback only; an item's explicit account remains authoritative.
            expense = self.category.default_expense_account
        if expense is not None and (
            not expense.is_active or not expense.is_postable or expense.account_type != "EXPENSE"
        ):
            # Historical master links remain readable but cannot seed an unusable posting account.
            expense = None
        tax = self.default_tax_code
        if tax is not None:
            paid = tax.paid_account
            usable_paid = paid is not None and paid.is_active and paid.is_postable and paid.account_type == "ASSET"
            if not tax.is_active or (tax.rate_bps and (not tax.is_recoverable or not usable_paid)):
                # Historical tax links stay visible but cannot seed a line that would fail posting.
                tax = None
        return {
            "description": self.description or self.name,
            "expense_account": expense,
            "tax_code": tax,
            "unit_price": self.standard_unit_price,
        }

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


# --------------------------------------------------------------------------- #
# Inventory / stock ledger (perpetual, weighted-average cost)                 #
# --------------------------------------------------------------------------- #

class StockLocation(TimeStampedModel):
    """Somewhere stock physically sits: a branch store, a lab, a kitchen.

    Stock used to be one pool per entity, which is wrong the moment a school has two
    branches. The pool told you a thousand books existed; it could not tell you that
    seven hundred were at one site and three hundred at the other, so an issue at the
    smaller site drew against stock it did not have and the availability check allowed
    it. Worse quietly: one blended average cost meant a branch that bought at a higher
    price and one that bought lower both issued at the middle, so each site's expense
    was wrong in opposite directions.

    ``branch`` is optional on purpose. A school with no branches has one location and
    the dimension recedes; a branch may hold several locations, because "which branch"
    and "which store on that branch" are different questions and the second one does
    not disappear just because a school is single-site.

    Exactly one location per entity carries ``is_default``, which is what lets a
    single-location entity keep calling the stock services without naming one.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT,
        related_name="stock_locations",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="stock_locations", null=True, blank=True,
        help_text="Branch this store belongs to. Blank for an entity-wide store.",
    )
    code = models.CharField(max_length=40, help_text="Location code, unique within the entity.")
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=255, blank=True, default="")
    is_default = models.BooleanField(
        default=False,
        help_text="The location used when a caller names none. One per entity.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_proc_stocklocation_entity_code",
            ),
            # One default per entity. A second would make "the default" ambiguous at
            # exactly the moment a caller relies on it.
            models.UniqueConstraint(
                fields=["entity"], condition=models.Q(is_default=True),
                name="uniq_proc_stocklocation_one_default",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["entity", "branch"]),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class StockItem(_AutoMasterCodeMixin, TimeStampedModel):
    """A physically stocked good - carries live on-hand quantity and its GL value.

    Distinct from :class:`CatalogItem`: a catalog item is *buying* master data (defaults
    that pre-fill purchase lines, including services you never hold), whereas a stock item
    is *inventory* state - what is physically held, counted, and carried on the balance
    sheet. The optional :attr:`catalog_item` link joins a stocked good to its buying
    defaults when one exists.

    Valuation is **weighted-average** held without floats: rather than storing a
    fractional unit cost, the item carries integer ``on_hand_qty`` and the total
    ``stock_value`` (kobo); the moving-average unit cost is *derived* (:attr:`unit_cost`).
    Each :class:`StockMovement` adjusts both atomically, so ``stock_value`` always equals
    the perpetual-inventory balance for this item in :attr:`inventory_account`.
    """

    AUTO_CODE_PREFIX = "ST"

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
    # Denormalized ledger balances: stock services update these under a row lock and
    # append the matching StockMovement. Ordinary model/API writes must not own them.
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
            # Import locally so the model layer does not create a module-load cycle with
            # stock services, while keeping every stock valuation on one rounding rule.
            from .stock import round_stock_kobo

            return round_stock_kobo(Decimal(self.stock_value) / Decimal(self.on_hand_qty))
        return 0

    @property
    def needs_reorder(self) -> bool:
        """Whether the live balance has reached the item's inclusive reorder point."""
        return self.on_hand_qty <= self.reorder_level

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class StockBalance(TimeStampedModel):
    """What one stock item holds at one location, and what it is worth there.

    This is where the perpetual sub-ledger actually lives now. The item's own
    ``on_hand_qty`` and ``stock_value`` are kept as the roll-up across every location,
    so the reports, serializers and reorder logic that read them continue to read the
    same numbers; the difference is that those numbers are now a sum of these rows
    rather than the only record.

    Weighted-average cost is held per row, which is the correctness point: a branch
    that bought at a higher price values its own stock at that price instead of at a
    blend with the other branch.
    """

    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="balances",
    )
    location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="balances",
    )
    on_hand_qty = models.DecimalField(
        max_digits=16, decimal_places=4, default=0,
        help_text="Live quantity at this location (maintained by the stock ledger).",
    )
    # Denormalized, exactly like the item roll-up above it: the stock services update
    # this under a row lock and append the matching StockMovement. Ordinary model or
    # API writes must not own it.
    stock_value = MoneyField(
        help_text="Value of stock at this location, in kobo (weighted-average basis).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stock_item", "location"],
                name="uniq_proc_stockbalance_item_location",
            ),
        ]
        indexes = [
            models.Index(fields=["location", "stock_item"]),
        ]
        ordering = ["stock_item", "location"]

    @property
    def unit_cost(self) -> int:
        """Weighted-average unit cost at this location, in kobo (0 when empty)."""
        if self.on_hand_qty and self.on_hand_qty > 0:
            from .stock import round_stock_kobo

            return round_stock_kobo(Decimal(self.stock_value) / Decimal(self.on_hand_qty))
        return 0

    def __str__(self) -> str:
        return f"{self.stock_item_id}@{self.location_id}"


class StockMovement(TimeStampedModel):
    """One immutable line of the perpetual stock ledger for a :class:`StockItem`.

    Signed in both quantity and value (``+`` in, ``-`` out) so a running sum reproduces
    the on-hand balance. ``balance_qty`` / ``balance_value`` snapshot the state of this
    movement's **location** afterwards, so the history reconstructs one site's position
    rather than a blend of every site. ``journal`` links the GL entry the movement
    posted (a stock-tracked GRN line, an issue, or an adjustment).
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="stock_movements",
    )
    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="movements",
    )
    # Nullable only so historical rows can be backfilled by migration; every movement
    # written from now on carries one.
    location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="movements",
        null=True, blank=True,
        help_text="Where the stock moved. Set on every movement written after 0028.",
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

    # SET_NULL preserves the immutable movement even if a non-posted/draft receipt is
    # later removed; the movement's reference, balances, and protected journal survive.
    grn = models.ForeignKey(
        "GoodsReceivedNote", on_delete=models.SET_NULL, related_name="stock_movements",
        null=True, blank=True,
    )
    # PROTECT the accounting proof even if surrounding procurement master data changes;
    # posted stock history must always retain the journal that valued it.
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
# Vendor contracts (master data - no GL effect)                               #
# --------------------------------------------------------------------------- #

class VendorContract(TimeStampedModel):
    """A term agreement with a vendor - the basis for renewal/expiry alerts.

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
# Purchase requisition (intent to buy - no GL effect)                         #
# --------------------------------------------------------------------------- #

class PurchaseRequisition(FinanceDocument):
    """An internal request to buy - the start of the procurement chain.

    No GL effect: a requisition is intent, approved (via ``vs_workflow``) and then
    converted into one or more :class:`PurchaseOrder` s. ``status`` uses the shared
    document lifecycle (DRAFT → PENDING_APPROVAL → APPROVED → CANCELLED).
    """

    DOC_TYPE = DocType.PURCHASE_REQUISITION
    #: vs_workflow integration - see vs_procurement.workflow_handlers / .approvals.
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
        """Roll line estimates into the denormalized requisition header total.

        The service layer calls this after line mutation. ``save=False`` supports a
        caller that is already assembling a wider atomic write.
        """
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
# Sourcing - RFQ → vendor quotations → award (no GL effect)                   #
# --------------------------------------------------------------------------- #

class RequestForQuotation(FinanceDocument):
    """A request inviting vendors to quote - competitive sourcing before a PO.

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
    response_due_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Exact UTC closing instant. Date-only RFQs default to 23:59:59 local time.",
    )
    version = models.PositiveSmallIntegerField(default=1)
    budget_estimate = MoneyField(
        null=True, blank=True, help_text="Optional buyer budget ceiling, in kobo.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "rfq_status"]),
            models.Index(fields=["entity", "issue_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.document_number or 'RFQ?'} · {self.title}"


class RfqInvitation(TimeStampedModel):
    """A vendor invited to quote on an RFQ - the RFQ's addressee list.

    An RFQ is fundamentally a *request for quotation sent to invited vendors*, so this
    join row records exactly which vendors were asked to bid. Invited-vendor semantics:
    a vendor may submit a :class:`VendorQuotation` against an RFQ **only if it holds an
    invitation on that RFQ** (enforced in the quotation-create view and defensively in
    :func:`vs_procurement.sourcing.submit_quotation`).

    There is deliberately **no status field**: "responded" is *derived* from whether a
    quotation exists from this vendor on this RFQ, so the invitation stays a pure
    addressee record that can never drift out of sync with the real quotations.
    """

    # CASCADE: an invitation is meaningless without its RFQ and carries no GL weight, so
    # deleting a draft RFQ takes its invitations with it.
    rfq = models.ForeignKey(
        RequestForQuotation, on_delete=models.CASCADE, related_name="invitations",
    )
    # PROTECT: who was invited to quote is part of the sourcing audit trail; deleting a
    # vendor must not silently erase the record that it was asked.
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="rfq_invitations",
    )
    status = models.CharField(
        max_length=10, choices=RfqInvitationStatus.choices,
        default=RfqInvitationStatus.PENDING,
    )
    token_version = models.PositiveSmallIntegerField(default=1)
    extended_deadline = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    draft_started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=500, blank=True, default="")
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    reminder_stage = models.PositiveSmallIntegerField(default=0)
    acknowledged_version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        # A vendor is either invited to an RFQ or not - never invited twice.
        unique_together = (("rfq", "vendor"),)
        ordering = ["rfq", "id"]
        indexes = [models.Index(fields=["rfq"])]

    def __str__(self) -> str:
        return f"RFQ {self.rfq_id} → {self.vendor_id}"

    @property
    def deadline(self):
        return self.extended_deadline or self.rfq.response_due_at


class RfqInvitationRecipient(TimeStampedModel):
    """Snapshot of a vendor contact who received one invitation."""

    invitation = models.ForeignKey(
        RfqInvitation, on_delete=models.CASCADE, related_name="recipients",
    )
    contact = models.ForeignKey(
        VendorContact, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invitation_recipients",
    )
    name = models.CharField(max_length=160, blank=True, default="")
    email = models.EmailField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("email"), "invitation", name="uniq_proc_rfq_invite_recipient_email_ci",
            ),
        ]
        ordering = ["id"]


class RfqInvitationVerification(TimeStampedModel):
    """Short-lived hashed email code for a public invitation."""

    invitation = models.ForeignKey(
        RfqInvitation, on_delete=models.CASCADE, related_name="verification_codes",
    )
    email = models.EmailField()
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["invitation", "email", "expires_at"])]


class RfqInvitationSession(TimeStampedModel):
    """A 24-hour verified browser session for a vendor invitation."""

    invitation = models.ForeignKey(
        RfqInvitation, on_delete=models.CASCADE, related_name="sessions",
    )
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["invitation", "expires_at"])]


class RfqAmendment(TimeStampedModel):
    """Published change notice preserving the RFQ version a vendor answered."""

    rfq = models.ForeignKey(
        RequestForQuotation, on_delete=models.PROTECT, related_name="amendments",
    )
    version = models.PositiveSmallIntegerField()
    summary = models.CharField(max_length=500)
    response_required = models.BooleanField(default=True)
    published_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_rfq_amendments",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rfq", "version"], name="uniq_proc_rfq_amendment_version"),
        ]
        ordering = ["rfq", "version"]


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
    version = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

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
    vendor_managed = models.BooleanField(
        default=False,
        help_text="True when the external vendor portal owns the draft content.",
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
        """Roll quotation line net/tax values into its denormalized header totals."""
        # Quotation gross is the sum of line net values plus their calculated tax.
        agg = self.lines.aggregate(
            net=models.Sum(
                "net_amount", filter=~models.Q(response_type=QuotationLineResponse.NO_BID),
            ),
            tax=models.Sum(
                "tax_amount", filter=~models.Q(response_type=QuotationLineResponse.NO_BID),
            ),
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
    response_type = models.CharField(
        max_length=12, choices=QuotationLineResponse.choices,
        default=QuotationLineResponse.QUOTED,
    )
    alternative_for = models.ForeignKey(
        RfqLine, on_delete=models.PROTECT, related_name="alternative_quotation_lines",
        null=True, blank=True,
    )

    class Meta:
        ordering = ["quotation", "line_no", "id"]
        indexes = [models.Index(fields=["quotation"])]

    def __str__(self) -> str:
        return f"{self.description}: {self.quantity} @ {self.unit_price}"


def quotation_attachment_upload_to(instance, filename: str) -> str:
    """Keep vendor evidence grouped while storage adds an unguessable suffix."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename).rsplit("/", 1)[-1])
    return f"quotation-attachments/{instance.quotation_id}/v{instance.revision}/{safe}"


class VendorQuotationAttachment(TimeStampedModel):
    """A small vendor-supplied PDF or image tied to one quotation revision."""

    quotation = models.ForeignKey(
        VendorQuotation, on_delete=models.CASCADE, related_name="attachments",
    )
    revision = models.PositiveSmallIntegerField(default=1)
    file = models.FileField(upload_to=quotation_attachment_upload_to)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.PositiveIntegerField()
    uploaded_by_email = models.EmailField(blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["quotation", "revision"])]


class VendorQuotationSubmission(TimeStampedModel):
    """Immutable receipt snapshot for each firm vendor submission."""

    quotation = models.ForeignKey(
        VendorQuotation, on_delete=models.PROTECT, related_name="submissions",
    )
    revision = models.PositiveSmallIntegerField()
    rfq_version = models.PositiveSmallIntegerField()
    submitted_at = models.DateTimeField()
    submitted_by_email = models.EmailField()
    snapshot = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["quotation", "revision"], name="uniq_proc_quotation_submission_revision",
            ),
        ]
        ordering = ["quotation", "-revision"]


# --------------------------------------------------------------------------- #
# Purchase order (commitment - no GL effect until receipt)                    #
# --------------------------------------------------------------------------- #

class PurchaseOrder(FinanceDocument):
    """A commitment to buy from a :class:`Vendor` at agreed prices.

    Still no GL posting (a commitment, not an expense); the cost hits the ledger when
    goods are received. Lines track ``received_qty``/``invoiced_qty`` so the PO knows
    how far through fulfilment and billing it is (``received_pct``/``invoiced_pct``).
    """

    DOC_TYPE = DocType.PURCHASE_ORDER
    #: vs_workflow integration - see vs_procurement.workflow_handlers / .approvals.
    workflow_document_type = WF_DOCTYPE_PURCHASE_ORDER
    workflow_amount_field = "total"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    requisition = models.ForeignKey(
        PurchaseRequisition, on_delete=models.PROTECT, related_name="purchase_orders",
        null=True, blank=True,
    )
    contract = models.ForeignKey(
        "VendorContract", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="purchase_orders",
        help_text=(
            "Explicit call-off link to the vendor contract this PO is raised against. "
            "SET_NULL so a contract deletion never blocks the order; optional so "
            "requisition-/award-spawned POs stay unlinked. The Contracts 'Linked POs' "
            "tab falls back to a vendor+term association only for POs where this is null."
        ),
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
        """Roll priced lines into the PO's denormalized net, tax, and gross totals."""
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


def purchase_order_vendor_pdf_path(instance, filename):
    """Keep generated vendor copies grouped by tenant, entity, and purchase order."""
    po = instance.purchase_order
    return (
        f"procurement/po-emails/{po.entity.tenant_id}/{po.entity_id}/"
        f"{po.pk}/{instance.pk or 'new'}/{filename}"
    )


class PurchaseOrderVendorDelivery(TimeStampedModel):
    """Durable schedule, attempt, and outcome for a purchase-order vendor email."""

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="vendor_deliveries",
    )
    source = models.CharField(
        max_length=16, choices=PurchaseOrderVendorDeliverySource.choices,
    )
    status = models.CharField(
        max_length=24, choices=PurchaseOrderVendorDeliveryStatus.choices,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requested_purchase_order_vendor_deliveries",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="retries",
    )
    workflow_instance_id = models.CharField(max_length=64, blank=True, default="")
    buyer_message = models.TextField(blank=True, default="")
    recipients = models.JSONField(default=list)
    # Monitoring copies are blind: these addresses are ours, not the vendor's.
    bcc = models.JSONField(default=list)
    notification_ids = models.JSONField(default=list)
    pdf_file = models.FileField(upload_to=purchase_order_vendor_pdf_path, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["purchase_order", "-created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_order"],
                condition=models.Q(status=PurchaseOrderVendorDeliveryStatus.AWAITING_APPROVAL),
                name="uniq_proc_po_active_email_intent",
            ),
        ]


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
    # Both progress counters are service-owned denormalizations. Receipt/invoice posting
    # advances them under transaction control; clients describe source quantities only.
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
        """Ordered quantity not yet accepted on posted receipts."""
        return Decimal(self.quantity) - Decimal(self.received_qty)

    @property
    def received_pct(self) -> Decimal:
        """Accepted receipt progress on the human 0–100 percentage scale."""
        return _pct(self.received_qty, self.quantity)

    @property
    def invoiced_pct(self) -> Decimal:
        """Billed progress on the human 0–100 percentage scale."""
        return _pct(self.invoiced_qty, self.quantity)

    def __str__(self) -> str:
        return f"{self.description}: {self.quantity} @ {self.unit_price}"


# --------------------------------------------------------------------------- #
# Goods received note (posts Dr expense, Cr GR/IR)                            #
# --------------------------------------------------------------------------- #

class GoodsReceivedNote(FinanceDocument):
    """A record that goods/services arrived - the first GL event in the chain.

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
        """Roll accepted line values into the receipt's denormalized kobo total."""
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
    """A bill from a :class:`Vendor` - the AP-side mirror of a sales invoice.

    Posting (:func:`vs_procurement.payables.post_vendor_invoice`) runs the three-way
    match, then raises the AP journal: **Dr GR/IR clearing** (clearing what receipt
    parked there) **+ Dr input VAT** (recoverable), **Cr AP control** (the gross owed).
    A non-PO bill debits the expense account directly instead of GR/IR. ``match_status``
    captures the match outcome; ``payment_status`` tracks cash settled, like AR.
    """

    DOC_TYPE = DocType.VENDOR_INVOICE
    #: vs_workflow integration - see vs_procurement.workflow_handlers / .approvals.
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
    creation_idempotency_key = models.CharField(
        max_length=128, blank=True, default="", editable=False,
        help_text="Client retry key used to return the original invoice instead of creating another.",
    )
    creation_request_hash = models.CharField(
        max_length=64, blank=True, default="", editable=False,
        help_text="SHA-256 of the creation payload bound to the idempotency key.",
    )
    narration = models.CharField(max_length=255, blank=True, default="")

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    amount_paid = MoneyField(help_text="Cash allocated to this bill, in kobo.")
    # Settlement fields are allocation-owned denormalizations, not editable payment
    # instructions. Posting/reversal services advance them from durable allocations.
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )
    match_status = models.CharField(
        max_length=16, choices=MatchStatus.choices, default=MatchStatus.NOT_MATCHED,
    )
    # Matching and approval are independent gates over the shared document lifecycle:
    # matching services own match_status; vs_workflow owns approval_state.
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
        constraints = FinanceDocument.Meta.constraints + [
            models.UniqueConstraint(
                Lower("vendor_reference"), "entity", "vendor",
                condition=~models.Q(vendor_reference=""),
                name="uniq_proc_vinvoice_entity_vendor_ref_ci",
            ),
            models.UniqueConstraint(
                fields=["entity", "creation_idempotency_key"],
                condition=~models.Q(creation_idempotency_key=""),
                name="uniq_proc_vinvoice_entity_idempotency_key",
            ),
        ]
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
        """Roll invoice line net/tax values into the authoritative gross payable."""
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
        """Refresh the settlement label from allocation-owned ``amount_paid``.

        Posting status remains independent: an invoice can be POSTED in the ledger while
        its AP settlement lifecycle is UNPAID, PARTIAL, or PAID.
        """
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


def vendor_invoice_attachment_path(instance, filename: str) -> str:
    """Group supplier evidence by tenant, entity, and bill; storage adds the token."""
    invoice = instance.vendor_invoice
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename).rsplit("/", 1)[-1])
    return (
        f"procurement/invoice-attachments/{invoice.entity.tenant_id}/"
        f"{invoice.entity_id}/{invoice.pk}/{safe}"
    )


class VendorInvoiceAttachment(TimeStampedModel):
    """The supplier's own bill - their PDF or a photo of it - held against ours.

    Evidence, not accounting: nothing here participates in matching or posting, and a
    bill with no attachment is still a valid bill. It exists because a recurring
    supplier charge that our books record only as a ``vendor_reference`` string has no
    paper trail an auditor (or the person who paid it) can follow back to the source.

    Attachable at any point in the bill's life, including after POSTED. A supplier
    frequently sends the formal invoice after the charge has already been booked, and
    a document that locks its own evidence out at posting time collects nothing.
    """

    vendor_invoice = models.ForeignKey(
        VendorInvoice, on_delete=models.CASCADE, related_name="attachments",
    )
    file = models.FileField(upload_to=vendor_invoice_attachment_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.PositiveIntegerField()
    caption = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="vendor_invoice_attachments", null=True, blank=True,
    )

    class Meta:
        ordering = ["vendor_invoice", "id"]
        indexes = [models.Index(fields=["vendor_invoice"])]

    def __str__(self) -> str:
        return f"{self.vendor_invoice_id}: {self.original_name}"


# --------------------------------------------------------------------------- #
# Vendor payment (posts Dr AP, Cr Bank net, Cr WHT)                           #
# --------------------------------------------------------------------------- #

class VendorPayment(FinanceDocument):
    """Money out to a :class:`Vendor`, settling one or more bills - with WHT.

    Posting (:func:`vs_procurement.payables.post_vendor_payment`) debits AP for the
    **gross** settled, credits the bank/cash for the **net** actually paid, and credits
    **WHT payable** for the tax withheld (``gross = net + wht``). The gross is then
    allocated across vendor invoices (the AP-side mirror of receipt allocation).
    """

    DOC_TYPE = DocType.VENDOR_PAYMENT
    #: vs_workflow integration - approval is separate from posting status.
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
    # The posting/allocation service owns this denormalized counter. Draft allocation
    # rows are instructions and do not become authoritative until posting succeeds.
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
        """Gross not yet applied to any bill - money sitting in vendor advances (1240).

        Named for the sub-ledger question ("how much of this payment has found a
        bill?"), but it is also a GL statement: whatever is unallocated was never
        debited to AP, it was debited to the vendor-advance asset and is still there.
        :attr:`advance_remaining` is the same figure named for that second question.
        """
        # Unallocated cash remains the gross payment less allocations already attached to invoices.
        return self.gross_amount - self.allocated_amount

    @property
    def advance_remaining(self) -> int:
        """Kobo of this payment still sitting in the vendor-advance asset (1240).

        The AP-side twin of :attr:`vs_finance.models.Payment.credit_remaining`, and the
        figure screens must show as "paid in advance". It equals
        :attr:`unallocated_amount` because allocation to a bill is the only thing that
        drains a vendor advance today; the AR twin subtracts refunds as well, and this
        property is where an AP-side refund would be subtracted if one is ever built.
        """
        return self.unallocated_amount


class VendorPaymentAllocation(TimeStampedModel):
    """Links a slice of a :class:`VendorPayment` (gross) to a :class:`VendorInvoice`.

    Mirrors :class:`vs_finance.models.PaymentAllocation`. Allocation is the sub-ledger
    act of saying which bills a payment settles; on the paying document's own date the
    settled part already debited AP directly. What allocation adds is the later case: a
    payment made before the bill existed parked its money in the vendor-advance asset,
    and applying it to a bill raises a reclassification (Dr AP, Cr vendor advances).

    A row is an immutable **event**, not a running total, for the same reason the AR
    twin is: two tranches against one bill can debit AP on two different dates, and a
    single accumulating row could carry only one of those dates honestly - leaving
    every "as at" report between them disagreeing with the ledger.
    """

    # Allocations are lifecycle-owned children of the payment instruction, but invoices
    # are durable AP documents: removing a draft payment may remove its split; removing
    # an invoice must never silently erase settlement history.
    payment = models.ForeignKey(
        VendorPayment, on_delete=models.CASCADE, related_name="allocations",
    )
    vendor_invoice = models.ForeignKey(
        VendorInvoice, on_delete=models.PROTECT, related_name="allocations",
    )
    amount = MoneyField(help_text="Gross applied to this bill, in kobo.")
    effective_date = models.DateField(
        null=True, blank=True,
        help_text="Accounting date this settlement took effect - the date of the "
                  "journal that debited AP for it. Null only on rows predating the "
                  "column, where it is reconstructed as max(payment date, bill date).",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_proc_alloc_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["vendor_invoice"]),
            models.Index(fields=["payment"]),
            models.Index(fields=["effective_date"]),
        ]
        ordering = ["payment", "id"]

    def __str__(self) -> str:
        return f"{self.payment_id}→{self.vendor_invoice_id}: {self.amount}"


def vendor_payment_attachment_path(instance, filename: str) -> str:
    """Group settlement evidence by tenant, entity, and payment; storage adds the token."""
    payment = instance.payment
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename).rsplit("/", 1)[-1])
    return (
        f"procurement/payment-attachments/{payment.entity.tenant_id}/"
        f"{payment.entity_id}/{payment.pk}/{safe}"
    )


class VendorPaymentAttachment(TimeStampedModel):
    """Proof that the money moved: the supplier's receipt, or the bank's.

    The counterpart to :class:`VendorInvoiceAttachment` at the other end of the chain.
    A receipt necessarily arrives *after* the payment is posted, so unlike the draft
    fields on this document there is no lifecycle state in which attaching one is
    disallowed - refusing the upload on a POSTED payment would reject every receipt
    that actually exists.
    """

    payment = models.ForeignKey(
        VendorPayment, on_delete=models.CASCADE, related_name="attachments",
    )
    file = models.FileField(upload_to=vendor_payment_attachment_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.PositiveIntegerField()
    caption = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="vendor_payment_attachments", null=True, blank=True,
    )

    class Meta:
        ordering = ["payment", "id"]
        indexes = [models.Index(fields=["payment"])]

    def __str__(self) -> str:
        return f"{self.payment_id}: {self.original_name}"


class VendorAdvanceAllocationJournal(TimeStampedModel):
    """Durable link from a later vendor-advance draw-down to its GL journal.

    A payment made before its bill exists leaves its money in the vendor-advance asset
    (1240). Applying it to a bill later raises its own reclassification journal
    (Dr AP, Cr vendor advances), which is a GL effect the payment document owns but
    which is not ``VendorPayment.journal``. Recording the link is what lets a reversal
    unwind *all* of a payment's ledger effects instead of just the original disbursement.

    The AP mirror of :class:`vs_finance.models.CustomerCreditAllocationJournal`.
    """

    payment = models.ForeignKey(
        VendorPayment, on_delete=models.PROTECT, related_name="advance_allocation_journals",
    )
    journal = models.OneToOneField(
        "vs_finance.JournalEntry", on_delete=models.PROTECT,
        related_name="vendor_advance_allocation",
    )
    amount = MoneyField(help_text="Vendor advance reclassified to AP, in kobo.")

    class Meta:
        ordering = ["journal_id", "id"]

    def __str__(self) -> str:
        return f"{self.payment_id}→J{self.journal_id}: {self.amount}"


# --------------------------------------------------------------------------- #
# Vendor assessment (point-in-time scorecard - immutable audit record)         #
# --------------------------------------------------------------------------- #

#: Fixed scorecard weights (sum to 1.0). Weighted overall = Σ (score × weight).
VENDOR_ASSESSMENT_WEIGHTS = {
    "on_time_delivery": Decimal("0.35"),
    "quality_acceptance": Decimal("0.30"),
    "invoice_accuracy": Decimal("0.20"),
    "responsiveness": Decimal("0.15"),
}


class VendorAssessment(TimeStampedModel):
    """A point-in-time vendor scorecard - an immutable audit record.

    Captures four 0–100 criteria (on-time delivery, quality acceptance, invoice
    accuracy, responsiveness) scored by an ``assessor`` on a date. ``overall_score``
    and the letter ``grade`` are **computed** from :data:`VENDOR_ASSESSMENT_WEIGHTS`,
    never stored, so the banding can evolve without a data migration. Create-only:
    an assessment is never edited or deleted - a newer one supersedes it.
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="vendor_assessments",
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="assessments",
    )
    assessor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="vendor_assessments", null=True, blank=True,
    )
    assessment_date = models.DateField(default=datetime.date.today)

    on_time_delivery = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    quality_acceptance = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    invoice_accuracy = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    responsiveness = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)])

    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-assessment_date", "-id"]
        indexes = [models.Index(fields=["entity", "vendor"])]

    @property
    def overall_score(self) -> int:
        # Weighted mean of the four criteria, rounded half-up to a whole 0–100 score.
        total = sum(
            (Decimal(getattr(self, field)) * weight
             for field, weight in VENDOR_ASSESSMENT_WEIGHTS.items()),
            Decimal(0),
        )
        return int(total.to_integral_value(rounding=ROUND_HALF_UP))

    @property
    def grade(self) -> str:
        # Bands: A ≥ 90, B ≥ 76, otherwise C.
        score = self.overall_score
        if score >= 90:
            return "A"
        if score >= 76:
            return "B"
        return "C"

    def __str__(self) -> str:
        return f"{self.vendor_id} · {self.assessment_date} · {self.grade}"


# --------------------------------------------------------------------------- #
# Parked-approval override - the break-glass record                           #
# --------------------------------------------------------------------------- #

class ApprovalOverride(TimeStampedModel):
    """One audited release of a **parked** spend approval, recorded forever.

    A parked document (see :mod:`vs_procurement.approval_parking`) is one whose active
    approval stage has an empty approver snapshot: nobody is able to decide it. A holder
    of ``procurement.approval.override`` may release that stage without a vote, and this
    row is the durable evidence of that act: who, when, which document, what it was worth
    at the time, and why.

    **Why a dedicated table rather than columns on the documents.**
    :class:`~vs_procurement.constants.ProcApprovalState` has four values and genuinely
    cannot express "approved without review", so something new had to be stored. Fields
    on the documents would mean the same five columns on four models (requisition, PO,
    vendor invoice, vendor payment), null on virtually every row, four migrations, and no
    natural home for a *second* release when a two-stage ladder parks twice. A generic
    side table records one row per release event, keeps the documents' own schemas clean,
    and is trivially append-only: the whole point of the record is that it can never be
    tidied away, so :meth:`save` refuses updates outright.

    ``amount`` is the document's workflow amount **at the moment of the override**, copied
    rather than joined: the released document may legitimately change value afterwards, and
    the question an auditor asks is "how much did this person wave through?".
    """

    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT,
        related_name="procurement_approval_overrides",
        help_text="Ledger entity of the released document; the tenant isolation boundary.",
    )
    # The released document, generically. Mirrors how vs_workflow points at its own
    # documents, so one table covers every approvable procurement type without
    # procurement growing a fifth, sixth, … override table later.
    document_content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.PROTECT, related_name="+",
    )
    document_object_id = models.PositiveBigIntegerField()
    document = GenericForeignKey("document_content_type", "document_object_id")
    #: Denormalised ``workflow_document_type`` token, for filtering without a join.
    document_type = models.CharField(max_length=100, db_index=True)
    document_number = models.CharField(
        max_length=48, blank=True, default="",
        help_text="Reference as it read when released; the document may be renumbered.",
    )

    #: ``WorkflowInstance.id`` as a plain string, matching PurchaseOrderVendorDelivery -
    #: procurement records the engine's identifier without taking a hard cross-app FK.
    workflow_instance_id = models.CharField(max_length=64, blank=True, default="")
    #: Code of the stage that was released; it is the stage that never ran.
    stage_code = models.CharField(max_length=64, blank=True, default="")
    stage_attempt = models.PositiveIntegerField(default=1)

    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="procurement_approval_overrides",
        help_text="The human accountable for the release. Never null: the override is the human.",
    )
    amount = MoneyField(help_text="The document's approval amount when released, in kobo.")
    reason = models.TextField(
        help_text="The actor's own words, stored verbatim and never editable.",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["entity", "-created_at"]),
            # Serves the "approved without review" list filter, which looks up by
            # document family and primary key.
            models.Index(fields=["document_content_type", "document_object_id"]),
        ]

    def save(self, *args, **kwargs):
        """Create-only: an override record is evidence, so it is never rewritten.

        Enforced here rather than by convention because the reason and the actor are the
        entire value of the row - a later edit would turn the audit trail into a claim.
        A mistaken override is corrected by a new, separately audited action, exactly as
        the ledger corrects by reversal rather than by edit.
        """
        if self.pk is not None and not self._state.adding:
            from .exceptions import ApprovalOverrideError

            raise ApprovalOverrideError(
                "An approval override is an immutable audit record and cannot be edited.",
            )
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.document_type} {self.document_object_id} released by {self.overridden_by_id}"
