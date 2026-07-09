"""Operational finance: banking, expenses, petty cash, tax, payroll, budgets, fixed assets.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from ..constants import (
    AssetCategory,
    AssetStatus,
    BankLineStatus,
    BankMatchSource,
    BankReconStatus,
    BankStatementStatus,
    BudgetStatus,
    DepreciationMethod,
    DocType,
    InvoicePaymentStatus,
    PayrollRunStatus,
    SalaryCalcMethod,
    SalaryComponentKind,
    StatutoryType,
    TaxFilingFrequency,
    TaxFilingStatus,
    TaxObligationType,
)
from ..money import MoneyField
from .core import TimeStampedModel, LedgerEntity, FinanceDocument
from .gl import Account, CostCenter, Currency, FiscalYear, TaxCode

# ---------------------------------------------------------------------------
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close
# ---------------------------------------------------------------------------
#
# All of these are entity-scoped finance-core concepts that post through the same
# `post_journal` service and period-lock guards as everything else. Nothing here
# imports a product/school app; staff are referenced through the platform user model
# (already used for `created_by`/`posted_by`), and a bank account is just a 1:1 view
# onto a cash/bank GL account.


class BankAccount(TimeStampedModel):
    """A real-world bank (or cash) account, mapped 1:1 to a GL cash account.

    The ledger already tracks cash in a GL account (e.g. ``1100 Cash & Bank`` or a
    child of it); this model adds the banking-side metadata (bank name, number) and is
    the anchor for statement import and reconciliation. Money still only ever moves via
    journals against ``gl_account`` — this is not a second source of truth for balance.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="bank_accounts",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_bank_accounts", null=True, blank=True,
    )
    gl_account = models.OneToOneField(
        Account, on_delete=models.PROTECT, related_name="bank_account",
        help_text="The cash/bank GL account this maps to (1:1). All movement posts here.",
    )
    name = models.CharField(max_length=160, help_text="Friendly label, e.g. 'GTBank Operations'.")
    bank_name = models.CharField(max_length=120, blank=True, default="")
    account_number = models.CharField(max_length=34, blank=True, default="")
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="bank_accounts",
        null=True, blank=True, help_text="Defaults to the entity base currency.",
    )
    is_active = models.BooleanField(default=True)
    is_primary = models.BooleanField(
        default=False, help_text="The entity's main operating account (at most one).")
    is_primary_collection = models.BooleanField(
        default=False,
        help_text="The entity's primary fee-collection account — the one printed as "
                  "'pay to' on customer invoices/receipts. At most one per entity.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_bank_entity_name",
            ),
            # At most one primary collection account per entity (partial unique).
            models.UniqueConstraint(
                fields=["entity"], condition=models.Q(is_primary_collection=True),
                name="uniq_finance_primary_collection_per_entity",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.account_number or self.gl_account_id})"


class BankStatementLine(TimeStampedModel):
    """One line of an imported bank statement, awaiting reconciliation.

    ``amount`` is **signed** in kobo from *our* perspective: positive is money into the
    account (a GL **debit** to the cash account), negative is money out (a GL credit).
    A line is reconciled by pairing it with the matching cash-account
    :class:`JournalLine`; charges/credits the books don't yet know about get an
    *adjusting* journal first, then match.
    """

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.CASCADE, related_name="statement_lines",
    )
    statement = models.ForeignKey(
        "BankStatement", on_delete=models.SET_NULL, related_name="lines",
        null=True, blank=True,
        help_text="The imported statement batch this line belongs to.",
    )
    txn_date = models.DateField(help_text="Value/transaction date on the statement.")
    description = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    amount = MoneyField(help_text="Signed kobo: +inflow (GL debit), -outflow (GL credit).")
    status = models.CharField(
        max_length=10, choices=BankLineStatus.choices, default=BankLineStatus.UNMATCHED,
    )
    matched_line = models.ForeignKey(
        "JournalLine", on_delete=models.SET_NULL, related_name="bank_statement_lines",
        null=True, blank=True,
        help_text="The cash-account journal line this statement line reconciles to.",
    )
    match_source = models.CharField(
        max_length=12, choices=BankMatchSource.choices, blank=True, default="",
        help_text="How it was matched: auto, manual, or via an adjusting entry.",
    )
    adjusting_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.SET_NULL, related_name="bank_adjustments",
        null=True, blank=True,
        help_text="Journal raised to book an unrecorded charge/credit before matching.",
    )
    external_id = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Bank/provider line id, used to de-duplicate on import.",
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bank_account", "external_id"],
                condition=models.Q(external_id__gt=""),
                name="uniq_finance_bankline_external",
            ),
        ]
        indexes = [
            models.Index(fields=["bank_account", "status"]),
            models.Index(fields=["bank_account", "txn_date"]),
        ]
        ordering = ["bank_account", "txn_date", "id"]

    def __str__(self) -> str:
        return f"{self.txn_date} {self.amount} [{self.status}]"


class BankLineMatch(TimeStampedModel):
    """Links a statement line to a GL cash journal line in a **group** (many-to-one) match.

    The 1:1 case uses :attr:`BankStatementLine.matched_line`. A group match — one bank
    line that settles several ledger movements (e.g. a PSP settlement covering many
    receipts) — records each paired cash :class:`JournalLine` here instead; their signed
    amounts sum to the statement line's amount. Unmatching deletes these rows.
    """

    statement_line = models.ForeignKey(
        BankStatementLine, on_delete=models.CASCADE, related_name="line_matches",
    )
    journal_line = models.ForeignKey(
        "JournalLine", on_delete=models.PROTECT, related_name="bank_line_matches",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["statement_line", "journal_line"],
                name="uniq_finance_bank_line_match",
            ),
        ]
        indexes = [models.Index(fields=["journal_line"])]

    def __str__(self) -> str:
        return f"{self.statement_line_id}↔{self.journal_line_id}"


class BankStatement(TimeStampedModel):
    """An imported bank statement — a batch of lines for a period, with opening/closing.

    Grouping imported :class:`BankStatementLine`\\s under a statement gives the banking
    screen a per-period view (opening → closing) and a reconciliation target. The book
    side of truth is still the GL; this records what the *bank* reported.
    """

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.CASCADE, related_name="statements",
    )
    statement_date = models.DateField(help_text="Closing date of the statement period.")
    period_label = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Human label for the period, e.g. 'Apr 2026' or 'May 1–15'.")
    opening_balance = MoneyField(default=0, help_text="Bank-reported opening balance, kobo.")
    closing_balance = MoneyField(default=0, help_text="Bank-reported closing balance, kobo.")
    status = models.CharField(
        max_length=12, choices=BankStatementStatus.choices,
        default=BankStatementStatus.UPLOADED,
    )
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="bank_statements_imported", null=True, blank=True,
    )

    class Meta:
        indexes = [models.Index(fields=["bank_account", "statement_date"])]
        ordering = ["-statement_date", "-id"]

    def __str__(self) -> str:
        return f"{self.bank_account.name} statement {self.statement_date}"

    @property
    def line_count(self) -> int:
        return self.lines.count()


class BankReconciliation(TimeStampedModel):
    """A reconciliation run snapshot — the book vs statement balances at a point in time.

    Recorded each time auto/assisted reconciliation runs, so the screen can show a
    history (and an out-of-balance trail) without recomputing the past.
    """

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.CASCADE, related_name="reconciliations",
    )
    statement = models.ForeignKey(
        BankStatement, on_delete=models.SET_NULL, related_name="reconciliations",
        null=True, blank=True,
    )
    as_of_date = models.DateField()
    book_balance = MoneyField(default=0, help_text="GL cash-account balance, kobo.")
    statement_balance = MoneyField(default=0, help_text="Bank-reported balance, kobo.")
    difference = MoneyField(default=0, help_text="book − statement (signed kobo).")
    matched_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=BankReconStatus.choices, default=BankReconStatus.BALANCED,
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="bank_reconciliations", null=True, blank=True,
    )

    class Meta:
        indexes = [models.Index(fields=["bank_account", "created_at"])]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.bank_account.name} recon {self.as_of_date} [{self.status}]"


class ExpenseClaim(FinanceDocument):
    """A staff expense claim — staff acts as a one-off 'vendor' to be reimbursed.

    Posting raises ``Dr expense(s) (+ Dr input VAT), Cr accrued reimbursement`` — the
    liability owed to the employee. Settling it later (:func:`vs_finance.expenses.
    settle_expense_claim`) pays the employee: ``Dr accrued reimbursement, Cr bank``.
    Reuses :class:`InvoicePaymentStatus` for how much has been reimbursed.
    """

    DOC_TYPE = DocType.EXPENSE_CLAIM

    claimant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_expense_claims", null=True, blank=True,
        help_text="The employee being reimbursed.",
    )
    claimant_name = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Free-text name when the claimant isn't a platform user.",
    )
    claim_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="expense_claims",
        null=True, blank=True,
    )
    title = models.CharField(max_length=200, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    reimbursement_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="expense_claims",
        null=True, blank=True,
        help_text="Liability credited (accrued reimbursement). Defaults to 2400.",
    )
    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Recoverable input tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    amount_paid = MoneyField(help_text="Reimbursed so far, in kobo.")
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="expense_claims",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "payment_status"]),
            models.Index(fields=["claimant"]),
        ]

    @property
    def balance_due(self) -> int:
        return self.total - self.amount_paid

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        if self.amount_paid <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif self.amount_paid >= self.total:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])


class ExpenseClaimLine(TimeStampedModel):
    """One expense line of a claim → a GL expense account (+ optional recoverable tax)."""

    claim = models.ForeignKey(
        ExpenseClaim, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="expense_claim_lines",
        help_text="GL expense account debited for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="expense_claim_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Recoverable tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="expense_claim_lines",
        null=True, blank=True,
    )
    receipt = models.FileField(
        upload_to="expense-receipts/", null=True, blank=True,
        help_text="Supporting receipt (DB-backed storage). PDF or image.",
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["claim", "line_no", "id"]
        indexes = [models.Index(fields=["claim"]), models.Index(fields=["expense_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.expense_account_id}: {self.line_total}"


class PettyCashFund(TimeStampedModel):
    """A physical petty-cash float, mapped 1:1 to its own petty-cash GL account.

    Master data (like :class:`BankAccount`) — money only ever moves via journals against
    :attr:`gl_account`; this row adds the operational metadata (custodian, the fixed
    ``float_amount`` the imprest is restored to) and a live ``current_balance`` mirror of
    the cash physically on hand. The fund runs **perpetually**: each
    :class:`PettyCashVoucher` posts ``Dr expense, Cr petty cash`` as it is spent, and
    :func:`vs_finance.petty_cash.replenish_fund` tops the float back up
    (``Dr petty cash, Cr bank``). ``current_balance`` is a denormalised mirror **re-synced
    from the GL balance of ``gl_account`` after every operation** (the GL is the source of
    truth; the overdraw guard reads it live), so the two never silently drift.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="petty_cash_funds",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_petty_cash_funds", null=True, blank=True,
    )
    gl_account = models.OneToOneField(
        Account, on_delete=models.PROTECT, related_name="petty_cash_fund",
        help_text="The petty-cash GL account this float maps to (1:1). All movement posts here.",
    )
    name = models.CharField(max_length=160, help_text="Friendly label, e.g. 'Front-desk float'.")
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="petty_cash_funds", null=True, blank=True,
        help_text="The person accountable for the cash tin.",
    )
    custodian_name = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Free-text custodian when not a platform user.",
    )
    float_amount = MoneyField(
        help_text="The imprest/float the fund is restored to on replenishment, in kobo.",
    )
    current_balance = MoneyField(
        help_text="Live cash on hand, in kobo (maintained by the petty-cash ledger).",
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="petty_cash_funds",
        null=True, blank=True, help_text="Defaults to the entity base currency.",
    )
    last_replenished_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_pettycash_entity_name",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]

    @property
    def shortfall(self) -> int:
        """How much a replenishment would draw to restore the float (kobo, never negative)."""
        return max(self.float_amount - self.current_balance, 0)

    def __str__(self) -> str:
        return f"{self.name} ({self.current_balance} kobo on hand)"


class PettyCashVoucher(FinanceDocument):
    """A single small disbursement from a petty-cash fund, recorded against a voucher slip.

    Posting (perpetual) raises ``Dr expense(s) (+ Dr input VAT), Cr petty cash`` and lowers
    the fund's ``current_balance`` by the gross total. A voucher whose total exceeds the
    cash on hand is rejected (:class:`~vs_finance.exceptions.PettyCashOverdrawError`) — you
    cannot pay out more than is in the tin.
    """

    DOC_TYPE = DocType.PETTY_CASH_VOUCHER

    fund = models.ForeignKey(
        PettyCashFund, on_delete=models.PROTECT, related_name="vouchers",
    )
    voucher_date = models.DateField()
    payee = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Who the cash was paid to.",
    )
    spent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="petty_cash_vouchers", null=True, blank=True,
        help_text="The staff member who incurred the spend.",
    )
    narration = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="petty_cash_vouchers",
        null=True, blank=True,
    )
    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Recoverable input tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo (the cash paid out).")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="petty_cash_vouchers",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["fund"]),
        ]

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {self.total} kobo"


class PettyCashVoucherLine(TimeStampedModel):
    """One expense line of a petty-cash voucher → a GL expense account (+ optional tax)."""

    voucher = models.ForeignKey(
        PettyCashVoucher, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="petty_cash_voucher_lines",
        help_text="GL expense account debited for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="petty_cash_voucher_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Recoverable tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="petty_cash_voucher_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["voucher", "line_no", "id"]
        indexes = [models.Index(fields=["voucher"]), models.Index(fields=["expense_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.expense_account_id}: {self.line_total}"


class TaxObligation(TimeStampedModel):
    """Master data for a recurring statutory remittance the entity must file & pay.

    Maps a tax type (VAT / WHT / PAYE / pension …) to the GL **liability control account**
    whose accumulated balance is what gets remitted, plus the authority it is paid to and
    how often a return falls due. VAT additionally carries a ``recoverable_account`` (input
    VAT, an asset) that is netted off the output payable at filing time.

    Kept as data (not hard-coded in services) so an entity with a customised chart, or a
    new statutory levy, can be configured without a code change.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="tax_obligations",
    )
    code = models.CharField(max_length=24, help_text="Short slug, e.g. 'VAT', 'WHT', 'PAYE'.")
    name = models.CharField(max_length=160)
    obligation_type = models.CharField(
        max_length=12, choices=TaxObligationType.choices,
        help_text="Which statutory tax this obligation covers.",
    )
    liability_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_obligations",
        help_text="Liability control account whose balance is remitted (Dr on payment).",
    )
    recoverable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_obligations_recoverable",
        null=True, blank=True,
        help_text="Recoverable input account netted off at filing (VAT only). Usually 1300.",
    )
    authority_name = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Who the tax is paid to, e.g. 'FIRS', 'Lagos State IRS', a PFA.",
    )
    frequency = models.CharField(
        max_length=12, choices=TaxFilingFrequency.choices,
        default=TaxFilingFrequency.MONTHLY,
    )
    filing_day = models.PositiveSmallIntegerField(
        default=21,
        help_text="Day of the month after period end the return is due (e.g. 21 for VAT).",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_taxobligation_entity_code",
            ),
        ]
        ordering = ["entity", "code"]
        indexes = [models.Index(fields=["entity", "is_active"])]

    def __str__(self) -> str:
        return f"{self.code} → {self.authority_name or self.liability_account_id}"


class TaxFiling(FinanceDocument):
    """A single statutory return for one obligation over one period, with its remittance.

    Lifecycle ``DRAFT → FILED → PAID`` (perpetual ledger; the liability already sits in the
    control account from source transactions):

    * **Prepare** (:func:`vs_finance.tax_filing.prepare_filing`): derive the amount owed
      from the GL movement of the obligation's ``liability_account`` over the period (for
      VAT, less the recoverable input movement). No posting — a draft worksheet.
    * **File** (:func:`vs_finance.tax_filing.file_filing`): freeze the figures and submit.
      Posts a netting/penalty journal only if there is recoverable input to clear or a
      penalty/interest adjustment, so the liability account is left holding exactly
      ``amount_due``.
    * **Pay** (:func:`vs_finance.tax_filing.pay_filing`): ``Dr liability, Cr bank`` for the
      remittance; supports partial payment. Reuses :class:`InvoicePaymentStatus`.
    """

    DOC_TYPE = DocType.TAX_FILING

    obligation = models.ForeignKey(
        TaxObligation, on_delete=models.PROTECT, related_name="filings",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    filing_status = models.CharField(
        max_length=10, choices=TaxFilingStatus.choices, default=TaxFilingStatus.DRAFT,
    )
    gross_liability = MoneyField(help_text="Output/payable accrued in the period, in kobo.")
    recoverable_amount = MoneyField(help_text="Recoverable input netted off (VAT), in kobo.")
    adjustment_amount = MoneyField(
        help_text="Penalty / interest added at filing (increases amount due), in kobo.",
    )
    amount_due = MoneyField(help_text="gross_liability − recoverable_amount + adjustment, in kobo.")
    amount_paid = MoneyField(help_text="Remitted so far, in kobo.")
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )
    adjustment_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_filing_adjustments",
        null=True, blank=True,
        help_text="Expense account debited for a penalty/interest adjustment.",
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="tax_filings",
        null=True, blank=True,
    )
    filing_reference = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Authority's return/receipt number once filed.",
    )
    filed_at = models.DateField(null=True, blank=True)
    narration = models.CharField(max_length=255, blank=True, default="")
    filing_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="tax_filing_postings",
        null=True, blank=True,
        help_text="The netting/penalty journal posted at filing (if any).",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "filing_status"]),
            models.Index(fields=["obligation"]),
        ]

    @property
    def balance_due(self) -> int:
        return self.amount_due - self.amount_paid

    def recompute_due(self, *, save: bool = True) -> None:
        self.amount_due = self.gross_liability - self.recoverable_amount + self.adjustment_amount
        if save:
            self.save(update_fields=["amount_due", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        if self.amount_paid <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif self.amount_paid >= self.amount_due:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {self.amount_due} kobo"


class PayrollRun(FinanceDocument):
    """A batch payroll run — gross/PAYE/pension/net for many employees at once.

    Two postings (the classic payroll pair):

    * **Accrual** (:func:`vs_finance.payroll.post_payroll`):
      ``Dr salary expense (gross), Cr PAYE payable, Cr pension payable, Cr net wages
      payable`` — recognises the cost and parks each statutory/ net liability.
    * **Disbursement** (:func:`vs_finance.payroll.pay_payroll`):
      ``Dr net wages payable, Cr bank`` — clears the net-pay liability when employees
      are actually paid.
    """

    DOC_TYPE = DocType.PAYROLL_RUN

    pay_date = models.DateField(help_text="Date the run is accounted/posted on.")
    period_label = models.CharField(max_length=40, blank=True, default="", help_text="e.g. 'January 2026'.")
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="payroll_runs",
        null=True, blank=True,
    )
    run_status = models.CharField(
        max_length=10, choices=PayrollRunStatus.choices, default=PayrollRunStatus.DRAFT,
    )
    narration = models.CharField(max_length=255, blank=True, default="")

    salary_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_salary_runs",
        null=True, blank=True, help_text="Defaults to 5200 Salaries & Wages.",
    )
    paye_payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_paye_runs",
        null=True, blank=True, help_text="Defaults to 2310 PAYE Payable.",
    )
    pension_payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_pension_runs",
        null=True, blank=True, help_text="Defaults to 2320 Pension Payable.",
    )
    net_payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_net_runs",
        null=True, blank=True, help_text="Defaults to 2330 Net Wages Payable.",
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="payroll_runs",
        null=True, blank=True, help_text="Cash account disbursed from at pay time.",
    )

    gross_total = MoneyField()
    paye_total = MoneyField()
    pension_total = MoneyField()
    net_total = MoneyField()

    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="payroll_accruals",
        null=True, blank=True,
    )
    disbursement_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="payroll_disbursements",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "run_status"]),
            models.Index(fields=["entity", "pay_date"]),
        ]

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            gross=models.Sum("gross_amount"), paye=models.Sum("paye_amount"),
            pension=models.Sum("pension_amount"), net=models.Sum("net_amount"),
        )
        self.gross_total = agg["gross"] or 0
        self.paye_total = agg["paye"] or 0
        self.pension_total = agg["pension"] or 0
        self.net_total = agg["net"] or 0
        if save:
            self.save(update_fields=[
                "gross_total", "paye_total", "pension_total", "net_total", "updated_at",
            ])


class PayrollLine(TimeStampedModel):
    """One employee's pay for a run. ``net = gross - paye - pension`` (all kobo)."""

    run = models.ForeignKey(
        PayrollRun, on_delete=models.CASCADE, related_name="lines",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_payroll_lines", null=True, blank=True,
    )
    employee_name = models.CharField(max_length=160, blank=True, default="")
    gross_amount = MoneyField(help_text="Gross pay in kobo.")
    paye_amount = MoneyField(help_text="PAYE (employee income tax) withheld, in kobo.")
    pension_amount = MoneyField(help_text="Employee pension contribution withheld, in kobo.")
    net_amount = MoneyField(help_text="Take-home: gross - paye - pension, in kobo.")
    components = models.JSONField(
        default=list, blank=True,
        help_text="Payslip breakdown snapshot copied from the salary structure at "
                  "generation: [{name, kind, statutory_type, amount}]. Empty in flat mode.",
    )
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="payroll_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["run", "line_no", "id"]
        indexes = [models.Index(fields=["run"]), models.Index(fields=["employee"])]

    def __str__(self) -> str:
        return f"{self.employee_name or self.employee_id}: net {self.net_amount}"


class SalaryStructure(TimeStampedModel):
    """A reusable named pay template — the earning/deduction components that define how
    an employee's gross is split into tranches and what's withheld.

    Assigning a structure to an :class:`EmployeeSalary` *derives* that employee's PAYE,
    pension and net from their gross, instead of typing each figure by hand. A structure
    never posts; it only shapes the numbers a :class:`PayrollRun` copies into its lines.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="salary_structures",
    )
    name = models.CharField(max_length=120, help_text="e.g. 'Senior staff'.")
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["entity", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"],
                name="uniq_salary_structure_name_per_entity",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class SalaryComponent(TimeStampedModel):
    """One line of a :class:`SalaryStructure`: an earning tranche or a deduction, plus the
    rule (fixed kobo, % of gross, or % of basic) that derives its amount.

    Earnings are an informational split of the gross (Basic/Housing/…); deductions tagged
    PAYE or pension are what actually reduce gross to net and route the GL credit.
    """

    structure = models.ForeignKey(
        SalaryStructure, on_delete=models.CASCADE, related_name="components",
    )
    name = models.CharField(max_length=80, help_text="e.g. 'Basic', 'Housing', 'PAYE'.")
    kind = models.CharField(
        max_length=10, choices=SalaryComponentKind.choices,
        default=SalaryComponentKind.EARNING,
    )
    calc_method = models.CharField(
        max_length=20, choices=SalaryCalcMethod.choices,
        default=SalaryCalcMethod.PERCENT_OF_GROSS,
    )
    rate_bps = models.PositiveIntegerField(
        default=0, help_text="Rate in basis points for the percent methods (4000 = 40%).",
    )
    amount = MoneyField(default=0, help_text="Fixed amount in kobo, for the FIXED method.")
    is_basic = models.BooleanField(
        default=False,
        help_text="Earnings flagged basic form the base for '% of basic' components.",
    )
    statutory_type = models.CharField(
        max_length=10, choices=StatutoryType.choices, default=StatutoryType.NONE,
        help_text="For deductions: routes the amount to PAYE/pension payable + the return.",
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["structure", "sequence", "id"]
        indexes = [models.Index(fields=["structure"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


class EmployeeSalary(TimeStampedModel):
    """An employee's standard monthly pay — the roster a payroll run is generated from.

    Holds the recurring gross/PAYE/pension for each employee so a run can be raised
    for the whole active roster in one click, instead of typing every line. It never
    posts on its own; :func:`vs_finance.payroll.generate_run_from_roster` copies the
    active rows into a draft :class:`PayrollRun`.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="employee_salaries",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_employee_salaries", null=True, blank=True,
    )
    name = models.CharField(max_length=160, help_text="Employee name.")
    structure = models.ForeignKey(
        SalaryStructure, on_delete=models.PROTECT, related_name="employee_salaries",
        null=True, blank=True,
        help_text="If set, PAYE/pension/net are derived from the structure applied to gross; "
                  "the manual paye/pension fields below are then ignored.",
    )
    gross_amount = MoneyField(help_text="Standard monthly gross pay, in kobo.")
    paye_amount = MoneyField(default=0, help_text="Manual PAYE withheld (flat mode, no structure), in kobo.")
    pension_amount = MoneyField(default=0, help_text="Manual pension withheld (flat mode, no structure), in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="employee_salaries",
        null=True, blank=True,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]

    @property
    def net_amount(self) -> int:
        return self.gross_amount - self.paye_amount - self.pension_amount

    def __str__(self) -> str:
        return f"{self.name}: gross {self.gross_amount}"


class Budget(TimeStampedModel):
    """An entity's plan of GL amounts for a fiscal year, by account/cost-centre/period.

    Read-only against the ledger: a budget never posts. Budget-vs-actual
    (:func:`vs_finance.reports.budget_vs_actual`) compares each line to the
    :class:`AccountBalance` actuals. Approval **locks** the figures so the plan can't be
    quietly rewritten to flatter the variance.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="budgets",
    )
    code = models.CharField(
        max_length=48, blank=True, db_index=True,
        help_text="Auto-allocated reference, e.g. CFX-CODEX-BDG-2026-00001.",
    )
    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT, related_name="budgets",
    )
    name = models.CharField(max_length=160)
    status = models.CharField(
        max_length=10, choices=BudgetStatus.choices, default=BudgetStatus.DRAFT,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_budgets_approved", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "fiscal_year", "name"],
                name="uniq_finance_budget_entity_year_name",
            ),
        ]
        indexes = [models.Index(fields=["entity", "status"])]
        ordering = ["entity", "-fiscal_year__year", "name"]

    def __str__(self) -> str:
        return f"{self.name} [{self.status}]"

    @property
    def is_locked(self) -> bool:
        return self.status == BudgetStatus.APPROVED


class BudgetLine(TimeStampedModel):
    """A budgeted amount for one (account, cost-centre, period) cell of a budget."""

    budget = models.ForeignKey(
        Budget, on_delete=models.CASCADE, related_name="lines",
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="budget_lines",
    )
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="budget_lines",
        null=True, blank=True,
    )
    period_no = models.PositiveSmallIntegerField(help_text="1–12; the fiscal period within the year.")
    amount = MoneyField(help_text="Budgeted amount for this cell, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["budget", "account", "cost_center", "period_no"],
                name="uniq_finance_budgetline_cell",
            ),
        ]
        indexes = [models.Index(fields=["budget", "account"])]
        ordering = ["budget", "account", "period_no"]

    def __str__(self) -> str:
        return f"{self.account_id} P{self.period_no}: {self.amount}"


class FixedAsset(FinanceDocument):
    """A depreciable asset in the register, with a straight-line schedule.

    Acquisition optionally posts ``Dr PP&E, Cr bank/payable``. Each period's
    depreciation posts ``Dr depreciation expense, Cr accumulated depreciation`` (a
    contra-asset), driven by :class:`DepreciationSchedule` rows and fed into period
    close. ``accumulated_depreciation`` tracks the running total booked.
    """

    DOC_TYPE = DocType.FIXED_ASSET

    name = models.CharField(max_length=200)
    asset_code = models.CharField(max_length=40, blank=True, default="", help_text="Optional tag/serial.")
    category = models.CharField(
        max_length=20, choices=AssetCategory.choices, default=AssetCategory.OTHER,
        help_text="Register category (Vehicles, Buildings, IT equipment…).",
    )
    asset_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fixed_assets",
        null=True, blank=True, help_text="Capitalised cost account. Defaults to 1500 PP&E.",
    )
    accumulated_depreciation_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fixed_assets_accum_dep",
        null=True, blank=True, help_text="Contra-asset. Defaults to 1900.",
    )
    depreciation_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fixed_assets_dep_expense",
        null=True, blank=True, help_text="Expense account. Defaults to 5400.",
    )
    acquisition_date = models.DateField()
    cost = MoneyField(help_text="Capitalised cost in kobo.")
    salvage_value = MoneyField(help_text="Residual value at end of life, in kobo.")
    useful_life_months = models.PositiveIntegerField(help_text="Depreciable life in months.")
    method = models.CharField(
        max_length=20, choices=DepreciationMethod.choices,
        default=DepreciationMethod.STRAIGHT_LINE,
    )
    asset_status = models.CharField(
        max_length=20, choices=AssetStatus.choices, default=AssetStatus.DRAFT,
    )
    accumulated_depreciation = MoneyField(help_text="Total depreciation booked to date, in kobo.")
    acquisition_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="asset_acquisitions",
        null=True, blank=True,
    )
    disposal_date = models.DateField(null=True, blank=True)
    disposal_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="asset_disposals",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "asset_status"]),
            models.Index(fields=["entity", "acquisition_date"]),
        ]

    @property
    def depreciable_base(self) -> int:
        """Cost less salvage — the total to be spread over the asset's life (kobo)."""
        return max(self.cost - self.salvage_value, 0)

    @property
    def net_book_value(self) -> int:
        return self.cost - self.accumulated_depreciation


class DepreciationSchedule(TimeStampedModel):
    """One period's planned (then posted) depreciation charge for a :class:`FixedAsset`."""

    asset = models.ForeignKey(
        FixedAsset, on_delete=models.CASCADE, related_name="schedule",
    )
    seq = models.PositiveSmallIntegerField(help_text="1-based month index in the asset's life.")
    depreciation_date = models.DateField(help_text="Date this charge posts on.")
    amount = MoneyField(help_text="Depreciation for this period, in kobo.")
    is_posted = models.BooleanField(default=False)
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.SET_NULL, related_name="depreciation_charges",
        null=True, blank=True,
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "seq"], name="uniq_finance_depschedule_asset_seq",
            ),
        ]
        indexes = [
            models.Index(fields=["asset", "is_posted"]),
            models.Index(fields=["depreciation_date"]),
        ]
        ordering = ["asset", "seq"]

    def __str__(self) -> str:
        return f"{self.asset_id} #{self.seq} {self.amount} {'✓' if self.is_posted else ''}".strip()

