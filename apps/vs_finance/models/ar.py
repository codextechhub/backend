"""Accounts receivable: customers, invoices, payments, allocations.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from ..constants import (
    DocType,
    FeeAppliesTo,
    InvoicePaymentStatus,
    InvoiceSource,
    PaymentMethod,
)
from ..money import MoneyField
from .core import TimeStampedModel, LedgerEntity, FinanceDocument
from .gl import Account, CostCenter, Currency, TaxCode

# ---------------------------------------------------------------------------
# Phase 2 — Accounts Receivable (the revenue cycle)
# ---------------------------------------------------------------------------
#
# A deliberately **domain-neutral** AR core: a generic Customer is billed with a
# generic Invoice and settles with a generic Payment. Nothing here knows about
# students, parents, fees or terms — a school billing run is just one *source* that
# emits these same generic invoices (the adapter, behind a module flag, comes later).
# The link back to a domain record is a loose, nullable string reference so the ledger
# never imports the students app.


class Customer(TimeStampedModel):
    """A billable party (the AR sub-ledger account) for one entity.

    Generic on purpose: a customer may be a parent/student in a school tenant, a
    client in another, or an internal counterparty in Codex's own books. The optional
    ``source_type``/``source_id`` pair is a *loose* reference to the originating
    domain record (e.g. ``"vs_schools.Student"`` + the student's pk) — stored as plain
    strings, never an FK, so the ledger stays decoupled from any product app.

    ``receivable_account`` is the AR control account this customer's balance rolls up
    into; the customer itself is the sub-ledger detail behind that control.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="customers",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_customers", null=True, blank=True,
    )
    code = models.CharField(max_length=32, help_text="Customer code, unique within the entity.")
    name = models.CharField(max_length=200)
    billing_email = models.EmailField(blank=True, default="")
    billing_phone = models.CharField(max_length=32, blank=True, default="")
    billing_address = models.TextField(blank=True, default="")
    receivable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="ar_customers",
        null=True, blank=True,
        help_text="AR control account this customer's balance rolls into.",
    )
    opening_balance = MoneyField(help_text="Opening AR balance in kobo (informational; not auto-posted).")
    source_type = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Loose reference to the originating domain record's model, e.g. 'vs_schools.Student'.",
    )
    source_id = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_customer_entity_code",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["source_type", "source_id"]),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class Invoice(FinanceDocument):
    """A generic sales invoice raised against a :class:`Customer`.

    Extends :class:`FinanceDocument` (entity scope, ``CFX-…-INV-…`` number, status,
    ``created_by``). Money totals are held in kobo and recomputed from the lines.
    Posting (:func:`vs_finance.receivables.post_invoice`) raises the AR journal
    (Dr receivable, Cr revenue, Cr output tax) and links it via ``journal``.

    Two status axes: the inherited document ``status`` tracks the ledger lifecycle
    (DRAFT→POSTED→CANCELLED), while ``payment_status`` tracks cash settled, derived
    from ``amount_paid`` vs ``total`` as payments allocate.
    """

    DOC_TYPE = DocType.INVOICE

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="invoices",
    )
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="invoices",
        null=True, blank=True,
    )
    source = models.CharField(
        max_length=16, choices=InvoiceSource.choices, default=InvoiceSource.MANUAL,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    amount_paid = MoneyField(help_text="Cash allocated to this invoice, in kobo.")
    amount_credited = MoneyField(
        help_text="Non-cash reductions (credit notes, write-offs) applied to this "
                  "invoice, in kobo. Reduces the balance due without recording cash.",
    )
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )

    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="ar_invoices",
        null=True, blank=True, help_text="The AR journal raised when this invoice posted.",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "payment_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "invoice_date"]),
        ]

    @property
    def settled_amount(self) -> int:
        """Total settled (cash + non-cash credits/write-offs), in kobo."""
        return self.amount_paid + self.amount_credited

    @property
    def balance_due(self) -> int:
        """Outstanding amount in kobo (total minus cash paid and non-cash credits)."""
        return self.total - self.settled_amount

    def recompute_totals(self, *, save: bool = True) -> None:
        """Roll the line amounts up into subtotal/tax_total/total (kobo)."""
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        """Derive ``payment_status`` from amount settled (cash + credits) vs ``total``."""
        settled = self.settled_amount
        if settled <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif settled >= self.total:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])


class InvoiceLine(TimeStampedModel):
    """One billable line of an :class:`Invoice` → a GL revenue account (+ optional tax).

    ``net_amount`` (kobo) is ``quantity × unit_price`` and ``tax_amount`` is computed
    from the line's :class:`TaxCode` at post time; both are stored so the invoice
    total is a simple, auditable sum and never re-derived inconsistently.
    """

    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="invoice_lines",
        help_text="GL revenue account credited for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="invoice_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="invoice_lines",
        null=True, blank=True,
    )
    dimensions = models.JSONField(default=dict, blank=True)
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["invoice", "line_no", "id"]
        indexes = [models.Index(fields=["invoice"]), models.Index(fields=["revenue_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.revenue_account_id}: {self.line_total}"


class Payment(FinanceDocument):
    """A customer receipt — money in, settling one or more invoices.

    Extends :class:`FinanceDocument` (DOC_TYPE RECEIPT → ``CFX-…-RCP-…``). Posting
    (:func:`vs_finance.receivables.post_payment`) raises Dr bank/cash, Cr AR control,
    then allocates the cash across invoices (oldest-first or explicit). Any amount
    beyond what's allocated remains an unallocated **credit** on the customer.
    """

    DOC_TYPE = DocType.RECEIPT

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="payments",
    )
    payment_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="payments",
        null=True, blank=True,
    )
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    amount = MoneyField(help_text="Total received, in kobo.")
    allocated_amount = MoneyField(help_text="Portion allocated to invoices, in kobo.")
    deposit_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="customer_payments",
        null=True, blank=True,
        help_text="Bank/cash account debited (where the money landed).",
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="ar_payments",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "payment_date"]),
        ]

    @property
    def unallocated_amount(self) -> int:
        """Cash not yet applied to any invoice — an open credit on the customer."""
        return self.amount - self.allocated_amount


class PaymentAllocation(TimeStampedModel):
    """Links a slice of a :class:`Payment` to a specific :class:`Invoice`.

    The GL already moved when the payment posted (Dr bank, Cr AR); allocation is the
    *sub-ledger* act of saying which invoices that AR credit settles. This keeps
    partial payments and unallocated credit first-class without further GL postings.
    """

    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="allocations",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="allocations",
    )
    amount = MoneyField(help_text="Amount of the payment applied to this invoice, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "invoice"], name="uniq_finance_alloc_payment_invoice",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_finance_alloc_non_negative",
            ),
        ]
        indexes = [models.Index(fields=["invoice"]), models.Index(fields=["payment"])]
        ordering = ["payment", "id"]

    def __str__(self) -> str:
        return f"{self.payment_id}→{self.invoice_id}: {self.amount}"




class FeeStructure(TimeStampedModel):
    """A named, reusable billing template for an entity.

    A fee structure is a catalogue of charges; it holds no money itself. Calling
    :func:`vs_finance.fees.generate_invoices` materialises one posted :class:`Invoice`
    per selected customer from this structure's :class:`FeeItem` lines — the only place
    billing turns a template into real AR. ``applies_to`` classifies the counterparty
    type the template charges (customer / vendor / staff / general); this is a generic
    platform, so a structure is not tied to any school term.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="fee_structures",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_fee_structures", null=True, blank=True,
    )
    code = models.CharField(max_length=32, help_text="Unique within the entity.")
    name = models.CharField(max_length=200)
    applies_to = models.CharField(
        max_length=16, choices=FeeAppliesTo.choices, default=FeeAppliesTo.CUSTOMER,
        help_text="Counterparty type this template bills (customer / vendor / staff / general).",
    )
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_fee_structures_created", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_feestructure_entity_code",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"

    @property
    def total(self) -> int:
        """Sum of the item amounts in kobo (net of tax; tax is added at generation)."""
        return self.items.aggregate(t=models.Sum("amount"))["t"] or 0

    @property
    def tax_total(self) -> int:
        """Tax that would be added at generation, in kobo (per line: net × rate_bps)."""
        return sum(
            (it.amount * it.tax_code.rate_bps) // 10000
            for it in self.items.all() if it.tax_code_id
        )

    @property
    def total_with_tax(self) -> int:
        """Gross total per customer in kobo (net subtotal + tax)."""
        return self.total + self.tax_total


class FeeItem(TimeStampedModel):
    """One charge line of a :class:`FeeStructure` → a GL revenue account (+ optional tax)."""

    structure = models.ForeignKey(
        FeeStructure, on_delete=models.CASCADE, related_name="items",
    )
    code = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Optional short fee code/category, e.g. 'TUITION', 'BOARDING'.")
    description = models.CharField(max_length=255)
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fee_items",
        help_text="GL revenue account this fee credits when billed.",
    )
    amount = MoneyField(help_text="Charge amount per customer, in kobo (net of tax).")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="fee_items",
        null=True, blank=True,
    )
    is_optional = models.BooleanField(
        default=False,
        help_text="An opt-in charge (vs a required line). Informational for now — "
                  "generation bills every line.")
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["structure", "line_no", "id"]
        indexes = [models.Index(fields=["structure"])]

    def __str__(self) -> str:
        return f"{self.description}: {self.amount}"
