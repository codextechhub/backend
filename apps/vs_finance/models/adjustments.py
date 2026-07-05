"""AR adjustments: credit notes, refunds, concessions, payment plans.
"""
from __future__ import annotations

from django.db import models

from ..constants import (
    ConcessionKind,
    CreditNoteKind,
    DocType,
    InstallmentStatus,
    InvoicePaymentStatus,
    PaymentMethod,
    PaymentPlanFrequency,
    PaymentPlanStatus,
)
from ..money import MoneyField
from .core import TimeStampedModel, FinanceDocument
from .gl import Account, CostCenter, Currency, TaxCode
from .ar import Customer, Invoice

# ---------------------------------------------------------------------------
# Phase 4 — AR adjustments (credit/debit notes, refunds, write-offs)
# ---------------------------------------------------------------------------
#
# The other side of the revenue cycle: not every billed amount is collected as first
# raised. A *credit note* gives value back (returns, allowances, corrections), a *debit
# note* charges more, a *refund* hands cash back for an over-paid credit balance, and a
# *write-off* concedes a receivable as bad debt. All post through the same
# `post_journal` service; credit notes and write-offs reduce an invoice's balance via
# its `amount_credited` field rather than recording cash.


class CreditNote(FinanceDocument):
    """A credit or debit note against a :class:`Customer`'s receivable.

    ``kind`` selects direction (:class:`~vs_finance.constants.CreditNoteKind`): a CREDIT
    note reduces AR (``Dr revenue/returns + Dr output tax, Cr AR``) and may be applied
    to specific invoices like a non-cash payment; a DEBIT note increases AR
    (``Dr AR, Cr revenue + Cr output tax``) as a supplementary charge. The document
    number token follows the kind (``CRN`` vs ``DRN``). Money is kobo throughout.
    """

    DOC_TYPE = DocType.CREDIT_NOTE  # overridden per-instance for DEBIT notes (DRN)

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="credit_notes",
    )
    kind = models.CharField(
        max_length=6, choices=CreditNoteKind.choices, default=CreditNoteKind.CREDIT,
    )
    note_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="credit_notes",
        null=True, blank=True,
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="credit_notes",
        null=True, blank=True,
        help_text="Optional originating invoice this note relates to.",
    )

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax reversed/charged, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    allocated_amount = MoneyField(
        help_text="Portion of a CREDIT note applied to invoices, in kobo.",
    )
    # A DEBIT note is a supplementary charge that debits AR, so — like an invoice — it
    # is *settled* by receipts. ``amount_paid`` tracks cash allocated against it and
    # ``settlement_status`` mirrors Invoice.payment_status. Both are inert on CREDIT
    # notes (which are the ones with balances *given back*, tracked by allocated_amount).
    amount_paid = MoneyField(help_text="Cash allocated to this DEBIT note, in kobo.")
    settlement_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
        help_text="How much of a DEBIT note has been settled by receipts.",
    )

    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="credit_notes",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "kind"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "note_date"]),
        ]

    @property
    def is_debit(self) -> bool:
        return self.kind == CreditNoteKind.DEBIT

    @property
    def unallocated_amount(self) -> int:
        """Credit not yet applied to an invoice (CREDIT notes only)."""
        return self.total - self.allocated_amount

    @property
    def balance_due(self) -> int:
        """Outstanding amount a DEBIT note still owes (total minus cash allocated).

        Meaningful for DEBIT notes only; a CREDIT note never carries a balance due.
        """
        return self.total - self.amount_paid

    def refresh_settlement_status(self, *, save: bool = True) -> None:
        """Derive a DEBIT note's ``settlement_status`` from ``amount_paid`` vs total."""
        if self.amount_paid <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif self.amount_paid >= self.total:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.settlement_status = status
        if save:
            self.save(update_fields=["settlement_status", "updated_at"])

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def save(self, *args, **kwargs):
        # The document-number token tracks the note's direction (CRN vs DRN).
        if not self.document_number:
            self.DOC_TYPE = (
                DocType.DEBIT_NOTE if self.kind == CreditNoteKind.DEBIT
                else DocType.CREDIT_NOTE
            )
        return super().save(*args, **kwargs)


class CreditNoteLine(TimeStampedModel):
    """One line of a :class:`CreditNote` → a GL revenue/returns account (+ optional tax)."""

    note = models.ForeignKey(
        CreditNote, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="credit_note_lines",
        help_text="Revenue/returns account adjusted for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="credit_note_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="credit_note_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["note", "line_no", "id"]
        indexes = [models.Index(fields=["note"]), models.Index(fields=["revenue_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.revenue_account_id}: {self.line_total}"


class CreditNoteAllocation(TimeStampedModel):
    """Links a slice of a CREDIT :class:`CreditNote` to a specific :class:`Invoice`.

    The GL already moved when the note posted (Dr revenue, Cr AR); allocation is the
    sub-ledger act of saying which invoices that credit settles, mirroring
    :class:`PaymentAllocation`. It bumps the invoice's ``amount_credited``.
    """

    note = models.ForeignKey(
        CreditNote, on_delete=models.CASCADE, related_name="allocations",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="credit_allocations",
    )
    amount = MoneyField(help_text="Amount of the note applied to this invoice, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["note", "invoice"], name="uniq_finance_cnalloc_note_invoice",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_finance_cnalloc_non_negative",
            ),
        ]
        indexes = [models.Index(fields=["invoice"]), models.Index(fields=["note"])]
        ordering = ["note", "id"]

    def __str__(self) -> str:
        return f"{self.note_id}→{self.invoice_id}: {self.amount}"


class DebitNoteAllocation(TimeStampedModel):
    """Links a slice of a :class:`Payment` to a specific DEBIT :class:`CreditNote`.

    A DEBIT note debits AR when it posts (the customer owes it), so a receipt settles
    it exactly like an invoice: the GL already credits AR for the applied cash (in
    :func:`vs_finance.receivables._post_payment_atomic`); this row is the sub-ledger
    record of which debit note that cash cleared, and bumps the note's ``amount_paid``.
    The invoice-settlement twin is :class:`PaymentAllocation`.
    """

    payment = models.ForeignKey(
        "Payment", on_delete=models.CASCADE, related_name="debit_note_allocations",
    )
    note = models.ForeignKey(
        CreditNote, on_delete=models.PROTECT, related_name="settlements",
    )
    amount = MoneyField(help_text="Amount of the payment applied to this debit note, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "note"], name="uniq_finance_dnalloc_payment_note",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_finance_dnalloc_non_negative",
            ),
        ]
        indexes = [models.Index(fields=["note"]), models.Index(fields=["payment"])]
        ordering = ["payment", "id"]

    def __str__(self) -> str:
        return f"{self.payment_id}→DN{self.note_id}: {self.amount}"


class Refund(FinanceDocument):
    """A cash refund paid back to a :class:`Customer` for an over-paid credit balance.

    Posting (:func:`vs_finance.credit_notes.post_refund`) raises ``Dr customer credit
    (2140), Cr bank`` — paying out the customer's stored credit balance (not an open
    receivable), capped at their available credit.

    Args:
        customer: The customer receiving the refund.
        refund_date: The date the refund is paid.
        currency: The currency of the refund (optional, defaults to entity's).
        method: How the refund is paid (bank transfer, cash, etc.).
        amount: The amount refunded, in kobo.
        bank_account: The bank account the refund is paid from (optional).
        deposit_account: The GL account credited (where the money left from).
        reference: Optional reference for the refund.
        narration: Optional narration for the refund.
    """

    DOC_TYPE = DocType.REFUND

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="refunds",
    )
    refund_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="refunds",
        null=True, blank=True,
    )
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    amount = MoneyField(help_text="Amount refunded, in kobo.")
    bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="refunds",
        null=True, blank=True,
        help_text="Bank account the refund is paid from.",
    )
    deposit_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="customer_refunds",
        null=True, blank=True,
        help_text="Cash/bank GL account credited (where the money left from).",
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="ar_refunds",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "refund_date"]),
        ]


class Concession(FinanceDocument):
    """A non-cash reduction of a receivable — a discount, waiver or scholarship.

    Posting (:func:`vs_finance.installments.post_concession`) raises ``Dr discounts &
    allowances, Cr AR control`` for ``amount`` and clears that much of the linked
    invoice via :attr:`Invoice.amount_credited` — exactly like a targeted, single-line
    credit note, but tagged by :class:`~vs_finance.constants.ConcessionKind` for
    reporting (a school tenant's *scholarship*/*bursary* is just ``kind=SCHOLARSHIP``).

    Arguments:
        customer: The customer receiving the concession.
        invoice: The invoice whose balance this concession reduces.
        kind: The type of concession (discount, waiver, scholarship).
        concession_date: The date the concession is granted.
        amount: The amount of the receivable forgiven/discounted, in kobo.
        allowance_account: The contra-revenue/expense account debited (optional).
        reason: Optional reason for the concession.
    """

    DOC_TYPE = DocType.CONCESSION

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="concessions",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="concessions",
        help_text="The invoice whose balance this concession reduces.",
    )
    kind = models.CharField(
        max_length=12, choices=ConcessionKind.choices, default=ConcessionKind.DISCOUNT,
    )
    concession_date = models.DateField()
    amount = MoneyField(help_text="Amount of the receivable forgiven/discounted, in kobo.")
    allowance_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="concessions",
        null=True, blank=True,
        help_text="Contra-revenue/expense account debited. Defaults to 4910 "
                  "Discounts & Concessions Allowed.",
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="concessions",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "kind"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["invoice"]),
            models.Index(fields=["entity", "concession_date"]),
        ]


class PaymentPlan(FinanceDocument):
    """An installment schedule that spreads a receivable over dated installments.

    A pure scheduling overlay — it never posts to the GL. The invoice it references
    already sits in AR; the plan only says *when* the customer is expected to pay and
    *how much* each time, so reminders/dunning and progress tracking have something to
    measure against. Settlement is reflected by distributing the linked invoice's
    settled amount across installments oldest-first
    (:func:`vs_finance.installments.refresh_plan_progress`).
    """

    DOC_TYPE = DocType.PAYMENT_PLAN

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="payment_plans",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="payment_plans",
        null=True, blank=True,
        help_text="The invoice this plan settles (optional for a standalone plan).",
    )
    plan_status = models.CharField(
        max_length=10, choices=PaymentPlanStatus.choices, default=PaymentPlanStatus.DRAFT,
    )
    start_date = models.DateField(help_text="Due date of the first installment.")
    frequency = models.CharField(
        max_length=12, choices=PaymentPlanFrequency.choices,
        default=PaymentPlanFrequency.MONTHLY,
    )
    installment_count = models.PositiveSmallIntegerField(default=1)
    total_amount = MoneyField(help_text="Total amount being spread, in kobo.")
    baseline_settled = MoneyField(
        help_text="Invoice settlement (cash + non-cash credits) already applied when "
                  "the plan was activated. Excluded from installment progress so "
                  "pre-plan credits/waivers aren't miscounted as installment payments.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "plan_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["invoice"]),
            models.Index(fields=["entity", "start_date"]),
        ]

    @property
    def scheduled_total(self) -> int:
        """Sum of the installment amounts (should equal ``total_amount`` once built)."""
        return self.installments.aggregate(s=models.Sum("amount"))["s"] or 0

    @property
    def settled_total(self) -> int:
        """Sum settled across installments, in kobo."""
        return self.installments.aggregate(s=models.Sum("amount_settled"))["s"] or 0

    @property
    def outstanding_total(self) -> int:
        return self.total_amount - self.settled_total


class PaymentPlanInstallment(TimeStampedModel):
    """One dated installment of a :class:`PaymentPlan` (scheduling detail, no GL)."""

    plan = models.ForeignKey(
        PaymentPlan, on_delete=models.CASCADE, related_name="installments",
    )
    seq_no = models.PositiveSmallIntegerField(help_text="1-based position in the schedule.")
    due_date = models.DateField()
    amount = MoneyField(help_text="Amount due for this installment, in kobo.")
    amount_settled = MoneyField(help_text="Amount settled against this installment, in kobo.")
    status = models.CharField(
        max_length=8, choices=InstallmentStatus.choices, default=InstallmentStatus.PENDING,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "seq_no"], name="uniq_finance_installment_plan_seq",
            ),
        ]
        indexes = [
            models.Index(fields=["plan"]),
            models.Index(fields=["due_date", "status"]),
        ]
        ordering = ["plan", "seq_no", "id"]

    @property
    def balance(self) -> int:
        return self.amount - self.amount_settled

    def is_overdue(self, *, as_of=None) -> bool:
        """True if not fully settled and its due date has passed ``as_of`` (default today)."""
        import datetime as _dt

        ref = as_of or _dt.date.today()
        return self.balance > 0 and self.due_date < ref

    def __str__(self) -> str:
        return f"#{self.seq_no} due {self.due_date}: {self.amount} ({self.status})"


