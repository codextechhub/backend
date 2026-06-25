"""DRF serializers for the vs_finance read/action API.

These serialise the ledger's master data and documents (entities, accounts, fiscal
periods, journals, invoices). The financial **reports/statements** are dataclasses,
not models, so the views render those to plain dicts directly (see ``reports_api`` in
:mod:`vs_finance.views`) rather than through a ModelSerializer.

Money is always kobo (integer minor units); each money field is mirrored with a
``*_naira`` display string so a client never has to know the divisor.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.fls import FieldSecurityMixin

from .models import (
    Account,
    BankAccount,
    BankReconciliation,
    BankStatement,
    BankStatementLine,
    Budget,
    BudgetLine,
    Concession,
    CostCenter,
    CreditNote,
    CreditNoteLine,
    Currency,
    Customer,
    Dimension,
    DepreciationSchedule,
    DunningNotice,
    DunningPolicy,
    DunningStage,
    ExpenseClaim,
    ExpenseClaimLine,
    FeeItem,
    FeeStructure,
    FinanceAuditLog,
    FixedAsset,
    FiscalPeriod,
    FiscalYear,
    FxRate,
    Invoice,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    Payment,
    PaymentPlan,
    PaymentPlanInstallment,
    EmployeeSalary,
    PayrollLine,
    PayrollRun,
    SalaryComponent,
    SalaryStructure,
    PettyCashFund,
    PettyCashVoucher,
    PettyCashVoucherLine,
    Refund,
    TaxCode,
    TaxFiling,
    TaxObligation,
)
from .money import format_naira


class LedgerEntitySerializer(serializers.ModelSerializer):
    base_currency = serializers.CharField(source="base_currency_id", read_only=True)

    class Meta:
        model = LedgerEntity
        fields = [
            "id", "code", "name", "kind", "base_currency",
            "is_active", "source_school_id",
        ]


class LedgerEntityCreateSerializer(serializers.ModelSerializer):
    """Write serializer for provisioning a new set of books (super-admin only).

    ``code`` is normalised to uppercase (it appears verbatim inside every document
    number) and ``base_currency`` accepts the 3-letter currency code (its PK).
    """

    base_currency = serializers.PrimaryKeyRelatedField(
        queryset=Currency.objects.all(), required=False,
    )
    # Optional: which fiscal year to open. Defaults to the current calendar year.
    fiscal_year = serializers.IntegerField(required=False, write_only=True, min_value=2000)
    # Optional opening month (1–12). 1 = calendar Jan–Dec; 9 = a Sept–Aug school year
    # whose twelve periods roll into the next calendar year.
    fiscal_start_month = serializers.IntegerField(
        required=False, write_only=True, min_value=1, max_value=12,
    )

    class Meta:
        model = LedgerEntity
        fields = ["id", "code", "name", "kind", "base_currency", "source_school",
                  "fiscal_year", "fiscal_start_month"]
        extra_kwargs = {
            "kind": {"required": False},
            "source_school": {"required": False, "allow_null": True},
        }

    def validate_code(self, value):
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Entity code is required.")
        if LedgerEntity.objects.filter(code=code).exists():
            raise serializers.ValidationError(f"A ledger entity with code '{code}' already exists.")
        return code

    def create(self, validated_data):
        from django.db import transaction
        from django.utils import timezone

        from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year

        fiscal_year = validated_data.pop("fiscal_year", None)
        start_month = validated_data.pop("fiscal_start_month", 1)
        validated_data.setdefault("kind", LedgerEntity.Kind.TENANT)

        # Provision a fully usable set of books in one call: the entity, the default
        # currencies, a starter chart of accounts, and twelve open monthly periods.
        # This keeps the bootstrap API-driven (no CLI seed_finance step required).
        # fiscal_start_month lets a school open e.g. a Sept–Aug year.
        with transaction.atomic():
            entity = LedgerEntity.objects.create(
                is_active=True, activated_at=timezone.now(), **validated_data,
            )
            seed_currencies()
            seed_chart_of_accounts(entity)
            seed_fiscal_year(entity, year=fiscal_year, start_month=start_month)
        return entity

    def to_representation(self, instance):
        # Echo back the canonical read shape so the caller sees base_currency code.
        return LedgerEntitySerializer(instance, context=self.context).data


class AccountSerializer(serializers.ModelSerializer):
    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)
    # Net GL balance signed to the account's normal balance — populated from the
    # ``_bal_dr``/``_bal_cr`` annotations the chart-of-accounts view adds.
    balance = serializers.SerializerMethodField()
    # Sub-ledger role: AR/AP control account, or the cash & bank account.
    tag = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = [
            "id", "code", "name", "account_type", "normal_balance",
            "is_contra", "is_postable", "is_active", "parent_id", "parent_code",
            "subtype", "balance", "tag",
        ]

    def get_balance(self, obj):
        from .constants import NormalBalance

        dr = getattr(obj, "_bal_dr", None)
        cr = getattr(obj, "_bal_cr", None)
        if dr is None and cr is None:
            return None  # not annotated (e.g. picker queries) — omit
        net = (dr or 0) - (cr or 0)
        if obj.normal_balance != NormalBalance.DEBIT:
            net = -net
        return {"kobo": int(net), "naira": format_naira(int(net))}

    def get_tag(self, obj):
        if obj.id in self.context.get("control_ids", set()):
            return "CONTROL"
        if obj.id in self.context.get("cash_ids", set()):
            return "CASH"
        return None


class FiscalPeriodSerializer(serializers.ModelSerializer):
    fiscal_year = serializers.IntegerField(source="fiscal_year.year", read_only=True)

    class Meta:
        model = FiscalPeriod
        fields = [
            "id", "period_no", "name", "fiscal_year",
            "start_date", "end_date", "status", "closed_at",
        ]


class FiscalYearSerializer(serializers.ModelSerializer):
    class Meta:
        model = FiscalYear
        fields = ["id", "year", "start_date", "end_date", "status"]


class JournalLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    debit_naira = serializers.SerializerMethodField()
    credit_naira = serializers.SerializerMethodField()

    class Meta:
        model = JournalLine
        fields = [
            "id", "line_no", "account_id", "account_code", "account_name",
            "cost_center", "debit", "credit", "debit_naira", "credit_naira", "description",
        ]

    def get_debit_naira(self, obj) -> str:
        return format_naira(obj.debit)

    def get_credit_naira(self, obj) -> str:
        return format_naira(obj.credit)


class JournalEntryListSerializer(serializers.ModelSerializer):
    period = serializers.CharField(source="period.name", read_only=True, default=None)
    total_debit = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    created_by_id = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = JournalEntry
        fields = [
            "id", "document_number", "date", "period", "source",
            "status", "narration", "reference", "posted_at",
            "total_debit", "created_by", "created_by_id",
        ]

    def get_total_debit(self, obj) -> int:
        # The list view annotates `_total_debit` (one query); detail falls back to totals().
        val = getattr(obj, "_total_debit", None)
        return int(val) if val is not None else obj.totals()[0]

    def get_created_by(self, obj) -> str:
        u = getattr(obj, "created_by", None)
        if u is None:
            return "system"
        name = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip()
        return name or getattr(u, "email", "") or "system"


class JournalEntryDetailSerializer(JournalEntryListSerializer):
    lines = JournalLineSerializer(many=True, read_only=True)
    total_credit = serializers.SerializerMethodField()

    class Meta(JournalEntryListSerializer.Meta):
        fields = JournalEntryListSerializer.Meta.fields + [
            "lines", "total_credit", "reverses_id",
        ]

    def _totals(self, obj):
        cache = getattr(self, "_totals_cache", {})
        if obj.id not in cache:
            cache[obj.id] = obj.totals()
            self._totals_cache = cache
        return cache[obj.id]

    def get_total_credit(self, obj) -> int:
        return self._totals(obj)[1]


class DirectEntryLineSerializer(serializers.Serializer):
    """One line of a direct entry: an account and a one-sided amount (kobo)."""

    account = serializers.CharField(help_text="Account code within the entity, e.g. '1100'.")
    debit = serializers.IntegerField(required=False, default=0, min_value=0)
    credit = serializers.IntegerField(required=False, default=0, min_value=0)

    def validate(self, attrs):
        if attrs.get("debit") and attrs.get("credit"):
            raise serializers.ValidationError(
                "A line is one-sided: set either debit or credit, not both.")
        return attrs


class DirectEntryCreateSerializer(serializers.Serializer):
    """Write serializer for a direct journal entry (capital, loans, openings, adjustments).

    All amounts are integer minor units (kobo). The lines must balance.
    """

    date = serializers.DateField(required=False)
    narration = serializers.CharField(required=False, allow_blank=True, default="")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    lines = DirectEntryLineSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("At least one line is required.")
        debit = sum(line["debit"] for line in value)
        credit = sum(line["credit"] for line in value)
        if debit != credit:
            raise serializers.ValidationError(
                f"Entry must balance: debits {debit} ≠ credits {credit} (kobo).")
        if debit == 0:
            raise serializers.ValidationError("Direct entry total cannot be zero.")
        return value


class CustomerSerializer(serializers.ModelSerializer):
    """Read shape for a customer / payer (the AR sub-ledger party)."""

    receivable_account_code = serializers.CharField(
        source="receivable_account.code", read_only=True, default=None)
    receivable_account_name = serializers.CharField(
        source="receivable_account.name", read_only=True, default=None)
    opening_balance_naira = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id", "code", "name", "billing_email", "billing_phone", "billing_address",
            "receivable_account_code", "receivable_account_name", "opening_balance",
            "opening_balance_naira", "source_type", "source_id", "is_active",
        ]

    def get_opening_balance_naira(self, obj) -> str:
        return format_naira(obj.opening_balance)


class FeeItemSerializer(serializers.ModelSerializer):
    revenue_account_code = serializers.CharField(source="revenue_account.code", read_only=True)
    tax_code_value = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = FeeItem
        fields = [
            "id", "line_no", "code", "description", "revenue_account_code",
            "amount", "amount_naira", "tax_code_value", "is_optional",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class FeeStructureSerializer(serializers.ModelSerializer):
    items = FeeItemSerializer(many=True, read_only=True)
    total = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    tax_total = serializers.IntegerField(read_only=True)
    tax_total_naira = serializers.SerializerMethodField()
    total_with_tax = serializers.IntegerField(read_only=True)
    total_with_tax_naira = serializers.SerializerMethodField()
    applies_to_display = serializers.CharField(
        source="get_applies_to_display", read_only=True)
    # Usage/activity — only computed for the detail view (context with_usage=True),
    # so the list endpoint stays a single query per page.
    created_by_name = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    class Meta:
        model = FeeStructure
        fields = [
            "id", "code", "name", "applies_to", "applies_to_display",
            "description", "is_active", "items",
            "total", "total_naira", "tax_total", "tax_total_naira",
            "total_with_tax", "total_with_tax_naira",
            "created_at", "created_by_name", "usage",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_tax_total_naira(self, obj) -> str:
        return format_naira(obj.tax_total)

    def get_total_with_tax_naira(self, obj) -> str:
        return format_naira(obj.total_with_tax)

    def get_created_by_name(self, obj):
        u = obj.created_by
        if not u:
            return None
        name = " ".join(filter(None, [
            getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
        return name or getattr(u, "email", None)

    def get_usage(self, obj):
        """Invoices raised from this structure (reference 'FEE:<code>'). Detail only."""
        if not self.context.get("with_usage"):
            return None
        from .models import Invoice
        qs = Invoice.objects.filter(
            entity_id=obj.entity_id, reference=f"FEE:{obj.code}", status="POSTED")
        last = qs.order_by("-created_at").values_list("created_at", flat=True).first()
        return {"invoices_generated": qs.count(), "last_generated_at": last}


class InvoiceSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "invoice_date", "due_date", "status", "payment_status",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "balance_due", "reference", "narration",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)


class CreditNoteLineSerializer(serializers.ModelSerializer):
    revenue_account = serializers.CharField(source="revenue_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)

    class Meta:
        model = CreditNoteLine
        fields = [
            "id", "line_no", "description", "revenue_account",
            "quantity", "unit_price", "tax_code", "net_amount", "tax_amount",
        ]


class CreditNoteSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True, default=None)
    unallocated_amount = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    lines = CreditNoteLineSerializer(many=True, read_only=True)

    class Meta:
        model = CreditNote
        fields = [
            "id", "document_number", "kind", "customer_id", "customer_code",
            "customer_name", "invoice_id", "invoice_number", "note_date", "status",
            "subtotal", "tax_total", "total", "total_naira",
            "allocated_amount", "unallocated_amount", "reason", "reference", "lines",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)


class RefundSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = Refund
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "refund_date", "method", "status", "amount", "amount_naira",
            "bank_account_id", "reference", "narration",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class PaymentSerializer(serializers.ModelSerializer):
    """A customer receipt + its allocation state (for Receipts & Allocation)."""

    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    deposit_account_code = serializers.CharField(source="deposit_account.code", read_only=True, default=None)
    deposit_account_name = serializers.CharField(source="deposit_account.name", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()
    unallocated_amount = serializers.IntegerField(read_only=True)
    allocation_status = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "payment_date", "method", "amount", "amount_naira", "allocated_amount",
            "unallocated_amount", "allocation_status", "deposit_account_code",
            "deposit_account_name", "reference", "narration", "journal_id", "status",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)

    def get_allocation_status(self, obj) -> str:
        if obj.unallocated_amount <= 0:
            return "ALLOCATED"
        if obj.allocated_amount <= 0:
            return "UNALLOCATED"
        return "PARTIAL"


class ConcessionSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True)
    allowance_account = serializers.CharField(
        source="allowance_account.code", read_only=True, default=None,
    )
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = Concession
        fields = [
            "id", "document_number", "kind", "customer_id", "customer_code",
            "customer_name", "invoice_id", "invoice_number", "concession_date",
            "status", "amount", "amount_naira", "allowance_account",
            "reason", "reference",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class PaymentPlanInstallmentSerializer(serializers.ModelSerializer):
    balance = serializers.IntegerField(read_only=True)

    class Meta:
        model = PaymentPlanInstallment
        fields = [
            "id", "seq_no", "due_date", "amount", "amount_settled",
            "balance", "status",
        ]


class PaymentPlanSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(
        source="invoice.document_number", read_only=True, default=None,
    )
    scheduled_total = serializers.IntegerField(read_only=True)
    settled_total = serializers.IntegerField(read_only=True)
    outstanding_total = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    installments = PaymentPlanInstallmentSerializer(many=True, read_only=True)

    class Meta:
        model = PaymentPlan
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "invoice_id", "invoice_number", "plan_status", "start_date", "frequency",
            "installment_count", "total_amount", "total_naira",
            "scheduled_total", "settled_total", "outstanding_total",
            "notes", "installments",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total_amount)


class DunningStageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DunningStage
        fields = [
            "id", "level", "name", "min_days_overdue", "channel", "message",
        ]


class DunningPolicySerializer(serializers.ModelSerializer):
    stages = DunningStageSerializer(many=True, read_only=True)

    class Meta:
        model = DunningPolicy
        fields = ["id", "name", "is_active", "is_default", "stages"]


class DunningNoticeSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True)
    policy_name = serializers.CharField(source="policy.name", read_only=True, default=None)
    amount_due_naira = serializers.SerializerMethodField()

    class Meta:
        model = DunningNotice
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "invoice_id", "invoice_number", "policy_id", "policy_name", "stage_id",
            "level", "notice_date", "days_overdue", "amount_due", "amount_due_naira",
            "channel", "message", "notice_status", "sent_at",
        ]

    def get_amount_due_naira(self, obj) -> str:
        return format_naira(obj.amount_due)


# --------------------------------------------------------------------------- #
# Setup / reference data                                                      #
# --------------------------------------------------------------------------- #

class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ["code", "name", "symbol", "minor_unit", "is_active"]


class FxRateSerializer(serializers.ModelSerializer):
    base = serializers.CharField(source="base_id", read_only=True)
    quote = serializers.CharField(source="quote_id", read_only=True)

    class Meta:
        model = FxRate
        fields = ["id", "base", "quote", "rate", "as_of", "source"]


class TaxCodeSerializer(serializers.ModelSerializer):
    collected_account = serializers.CharField(
        source="collected_account.code", read_only=True, default=None)
    paid_account = serializers.CharField(
        source="paid_account.code", read_only=True, default=None)

    class Meta:
        model = TaxCode
        fields = [
            "id", "code", "name", "rate_bps", "is_recoverable",
            "collected_account", "paid_account", "is_active",
        ]


class CostCenterSerializer(serializers.ModelSerializer):
    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)

    class Meta:
        model = CostCenter
        fields = ["id", "code", "name", "parent_id", "parent_code", "is_active"]


class DimensionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dimension
        fields = ["id", "code", "name", "is_active"]


# --------------------------------------------------------------------------- #
# Banking                                                                     #
# --------------------------------------------------------------------------- #

class BankAccountSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    gl_account = serializers.CharField(source="gl_account.code", read_only=True)
    gl_account_name = serializers.CharField(source="gl_account.name", read_only=True)
    currency = serializers.CharField(source="currency_id", read_only=True, default=None)
    book_balance = serializers.SerializerMethodField()
    book_balance_naira = serializers.SerializerMethodField()
    unreconciled_count = serializers.SerializerMethodField()
    last_reconciled_at = serializers.SerializerMethodField()

    # FLS: the funding account number is sensitive — only holders of the
    # sensitive grant see it; everyone else gets the record with it stripped.
    read_permissions = {
        "account_number": "finance.bankaccount.view_sensitive",
    }

    class Meta:
        model = BankAccount
        fields = [
            "id", "name", "bank_name", "account_number",
            "gl_account", "gl_account_name", "gl_account_id", "currency",
            "is_active", "is_primary",
            "book_balance", "book_balance_naira", "unreconciled_count",
            "last_reconciled_at",
        ]

    def get_book_balance(self, obj):
        from .banking import gl_account_balance
        return gl_account_balance(obj.gl_account)

    def get_book_balance_naira(self, obj):
        from .banking import gl_account_balance
        return format_naira(gl_account_balance(obj.gl_account))

    def get_unreconciled_count(self, obj):
        from .constants import BankLineStatus
        return obj.statement_lines.filter(status=BankLineStatus.UNMATCHED).count()

    def get_last_reconciled_at(self, obj):
        last = obj.reconciliations.order_by("-created_at").values_list(
            "created_at", flat=True).first()
        return last


class BankStatementLineSerializer(serializers.ModelSerializer):
    amount_naira = serializers.SerializerMethodField()
    match_source_display = serializers.CharField(source="get_match_source_display", read_only=True)
    matched_reference = serializers.SerializerMethodField()

    class Meta:
        model = BankStatementLine
        fields = [
            "id", "bank_account_id", "statement_id", "txn_date", "description",
            "reference", "amount", "amount_naira", "status", "matched_line_id",
            "adjusting_journal_id", "match_source", "match_source_display",
            "matched_reference", "external_id", "reconciled_at",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)

    def get_matched_reference(self, obj):
        """The document number of the matched journal entry (or adjusting entry)."""
        if obj.adjusting_journal_id:
            return obj.adjusting_journal.document_number
        if obj.matched_line_id and obj.matched_line.entry_id:
            return obj.matched_line.entry.document_number
        return None


class BankStatementSerializer(serializers.ModelSerializer):
    line_count = serializers.IntegerField(read_only=True)
    opening_balance_naira = serializers.SerializerMethodField()
    closing_balance_naira = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = BankStatement
        fields = [
            "id", "statement_date", "period_label", "opening_balance",
            "opening_balance_naira", "closing_balance", "closing_balance_naira",
            "line_count", "status", "status_display",
        ]

    def get_opening_balance_naira(self, obj) -> str:
        return format_naira(obj.opening_balance)

    def get_closing_balance_naira(self, obj) -> str:
        return format_naira(obj.closing_balance)


class BankReconciliationSerializer(serializers.ModelSerializer):
    book_balance_naira = serializers.SerializerMethodField()
    statement_balance_naira = serializers.SerializerMethodField()
    difference_naira = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BankReconciliation
        fields = [
            "id", "as_of_date", "book_balance", "book_balance_naira",
            "statement_balance", "statement_balance_naira", "difference",
            "difference_naira", "matched_count", "status", "status_display",
            "performed_by_name", "created_at",
        ]

    def get_book_balance_naira(self, obj) -> str:
        return format_naira(obj.book_balance)

    def get_statement_balance_naira(self, obj) -> str:
        return format_naira(obj.statement_balance)

    def get_difference_naira(self, obj) -> str:
        return format_naira(obj.difference)

    def get_performed_by_name(self, obj):
        u = obj.performed_by
        if not u:
            return None
        name = " ".join(filter(None, [
            getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
        return name or getattr(u, "email", None)


# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

class ExpenseClaimLineSerializer(serializers.ModelSerializer):
    expense_account = serializers.CharField(source="expense_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    line_total = serializers.IntegerField(read_only=True)
    receipt_name = serializers.SerializerMethodField()
    receipt_url = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseClaimLine
        fields = [
            "id", "line_no", "description", "expense_account", "quantity",
            "unit_price", "tax_code", "net_amount", "tax_amount", "line_total",
            "cost_center", "receipt_name", "receipt_url",
        ]

    def get_receipt_name(self, obj):
        if not obj.receipt:
            return None
        return obj.receipt.name.rsplit("/", 1)[-1]

    def get_receipt_url(self, obj):
        if not obj.receipt:
            return None
        request = self.context.get("request")
        url = obj.receipt.url
        return request.build_absolute_uri(url) if request else url


class ExpenseClaimSerializer(serializers.ModelSerializer):
    lines = ExpenseClaimLineSerializer(many=True, read_only=True)
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseClaim
        fields = [
            "id", "document_number", "claimant_id", "claimant_name", "claim_date",
            "title", "narration", "status", "payment_status",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "balance_due", "journal_id", "lines",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)


# --------------------------------------------------------------------------- #
# Petty cash                                                                  #
# --------------------------------------------------------------------------- #

class PettyCashFundSerializer(serializers.ModelSerializer):
    gl_account = serializers.CharField(source="gl_account.code", read_only=True)
    custodian_label = serializers.SerializerMethodField()
    float_amount_naira = serializers.SerializerMethodField()
    current_balance_naira = serializers.SerializerMethodField()
    shortfall = serializers.IntegerField(read_only=True)

    class Meta:
        model = PettyCashFund
        fields = [
            "id", "name", "gl_account", "gl_account_id",
            "custodian_id", "custodian_name", "custodian_label",
            "float_amount", "float_amount_naira",
            "current_balance", "current_balance_naira", "shortfall",
            "currency", "last_replenished_at", "is_active",
        ]

    def get_custodian_label(self, obj) -> str:
        if obj.custodian_id:
            full = obj.custodian.get_full_name() if hasattr(obj.custodian, "get_full_name") else ""
            return full or getattr(obj.custodian, "email", "") or str(obj.custodian_id)
        return obj.custodian_name

    def get_float_amount_naira(self, obj) -> str:
        return format_naira(obj.float_amount)

    def get_current_balance_naira(self, obj) -> str:
        return format_naira(obj.current_balance)


class PettyCashVoucherLineSerializer(serializers.ModelSerializer):
    expense_account = serializers.CharField(source="expense_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    line_total = serializers.IntegerField(read_only=True)

    class Meta:
        model = PettyCashVoucherLine
        fields = [
            "id", "line_no", "description", "expense_account", "quantity",
            "unit_price", "tax_code", "net_amount", "tax_amount", "line_total",
            "cost_center",
        ]


class PettyCashVoucherSerializer(serializers.ModelSerializer):
    lines = PettyCashVoucherLineSerializer(many=True, read_only=True)
    total_naira = serializers.SerializerMethodField()
    expense_account = serializers.SerializerMethodField()

    class Meta:
        model = PettyCashVoucher
        fields = [
            "id", "document_number", "fund_id", "voucher_date", "payee",
            "spent_by_id", "narration", "reference", "status",
            "subtotal", "tax_total", "total", "total_naira",
            "journal_id", "lines", "expense_account",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_expense_account(self, obj):
        """The voucher's expense category — the first line's account (code · name)."""
        lines = list(obj.lines.all()[:2])
        if not lines:
            return None
        acc = lines[0].expense_account
        label = f"{acc.code} · {acc.name}"
        return f"{label} +{len(lines) - 1}" if len(lines) > 1 else label


# --------------------------------------------------------------------------- #
# Tax remittance / filing                                                     #
# --------------------------------------------------------------------------- #

class TaxObligationSerializer(serializers.ModelSerializer):
    liability_account = serializers.CharField(source="liability_account.code", read_only=True)
    recoverable_account = serializers.CharField(
        source="recoverable_account.code", read_only=True, default=None,
    )

    class Meta:
        model = TaxObligation
        fields = [
            "id", "code", "name", "obligation_type",
            "liability_account", "liability_account_id",
            "recoverable_account", "recoverable_account_id",
            "authority_name", "frequency", "filing_day", "is_active",
        ]


class TaxFilingSerializer(serializers.ModelSerializer):
    obligation_code = serializers.CharField(source="obligation.code", read_only=True)
    obligation_type = serializers.CharField(source="obligation.obligation_type", read_only=True)
    authority_name = serializers.CharField(source="obligation.authority_name", read_only=True)
    balance_due = serializers.IntegerField(read_only=True)
    amount_due_naira = serializers.SerializerMethodField()

    class Meta:
        model = TaxFiling
        fields = [
            "id", "document_number", "obligation_id", "obligation_code",
            "obligation_type", "authority_name",
            "period_start", "period_end", "due_date",
            "filing_status", "status",
            "gross_liability", "recoverable_amount", "adjustment_amount",
            "amount_due", "amount_due_naira", "amount_paid", "balance_due",
            "payment_status", "adjustment_account_id",
            "filing_reference", "filed_at", "narration",
            "currency", "filing_journal_id",
        ]

    def get_amount_due_naira(self, obj) -> str:
        return format_naira(obj.amount_due)


# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #

class PayrollLineSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)

    # FLS: per-employee names and pay figures are sensitive — only holders of
    # the payroll sensitive grant see them; everyone else gets the line with
    # these fields stripped.
    read_permissions = {
        "employee_name": "finance.payrollrun.view_sensitive",
        "gross_amount": "finance.payrollrun.view_sensitive",
        "paye_amount": "finance.payrollrun.view_sensitive",
        "pension_amount": "finance.payrollrun.view_sensitive",
        "net_amount": "finance.payrollrun.view_sensitive",
        "components": "finance.payrollrun.view_sensitive",
    }

    class Meta:
        model = PayrollLine
        fields = [
            "id", "line_no", "employee_id", "employee_name",
            "gross_amount", "paye_amount", "pension_amount", "net_amount",
            "components", "cost_center",
        ]


class PayrollRunSerializer(serializers.ModelSerializer):
    lines = PayrollLineSerializer(many=True, read_only=True)
    net_total_naira = serializers.SerializerMethodField()
    # Statutory liability accounts the run credited (set on post) — let the FE match the
    # real outstanding balance (trial balance) to show remittance status honestly.
    paye_payable_account = serializers.CharField(source="paye_payable_account.code", read_only=True, default=None)
    pension_payable_account = serializers.CharField(source="pension_payable_account.code", read_only=True, default=None)

    class Meta:
        model = PayrollRun
        fields = [
            "id", "document_number", "pay_date", "period_label", "narration",
            "run_status", "status", "gross_total", "paye_total", "pension_total",
            "net_total", "net_total_naira", "bank_account_id",
            "paye_payable_account", "paye_payable_account_id",
            "pension_payable_account", "pension_payable_account_id",
            "journal_id", "disbursement_journal_id", "lines",
        ]

    def get_net_total_naira(self, obj) -> str:
        return format_naira(obj.net_total)


class SalaryComponentSerializer(serializers.ModelSerializer):
    """A structure line. Not FLS-stripped — a structure is configuration (e.g. 'Basic =
    40% of gross'), not any one person's pay."""

    class Meta:
        model = SalaryComponent
        fields = [
            "id", "name", "kind", "calc_method", "rate_bps", "amount",
            "is_basic", "statutory_type", "sequence",
        ]


class SalaryStructureSerializer(serializers.ModelSerializer):
    components = SalaryComponentSerializer(many=True, read_only=True)
    employee_count = serializers.SerializerMethodField()

    class Meta:
        model = SalaryStructure
        fields = [
            "id", "name", "description", "is_active", "components", "employee_count",
        ]

    def get_employee_count(self, obj) -> int:
        # annotated by the list view; fall back to a count for the detail view.
        cached = getattr(obj, "employee_count_annot", None)
        return cached if cached is not None else obj.employee_salaries.count()


class EmployeeSalarySerializer(FieldSecurityMixin, serializers.ModelSerializer):
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    structure_name = serializers.CharField(source="structure.name", read_only=True, default=None)
    # PAYE/pension/net/components are derived when a structure is assigned, else the stored
    # flat figures. Computed once per row (memoised) to avoid re-walking the components.
    paye_amount = serializers.SerializerMethodField()
    pension_amount = serializers.SerializerMethodField()
    net_amount = serializers.SerializerMethodField()
    components = serializers.SerializerMethodField()

    # FLS: the pay figures are sensitive — names stay visible (the roster), but the
    # amounts are stripped unless the caller holds the sensitive grant.
    read_permissions = {
        "gross_amount": "finance.payrollrun.view_sensitive",
        "paye_amount": "finance.payrollrun.view_sensitive",
        "pension_amount": "finance.payrollrun.view_sensitive",
        "net_amount": "finance.payrollrun.view_sensitive",
        "components": "finance.payrollrun.view_sensitive",
    }

    class Meta:
        model = EmployeeSalary
        fields = [
            "id", "name", "structure_id", "structure_name", "gross_amount",
            "paye_amount", "pension_amount", "net_amount", "components",
            "cost_center", "is_active",
        ]

    def _derived(self, obj) -> dict:
        cache = getattr(obj, "_derived_cache", None)
        if cache is None:
            from .payroll import apply_structure
            if obj.structure_id:
                cache = apply_structure(obj.gross_amount, obj.structure)
            else:
                cache = {
                    "paye": obj.paye_amount, "pension": obj.pension_amount,
                    "net": obj.net_amount, "components": [],
                }
            obj._derived_cache = cache
        return cache

    def get_paye_amount(self, obj) -> int:
        return self._derived(obj)["paye"]

    def get_pension_amount(self, obj) -> int:
        return self._derived(obj)["pension"]

    def get_net_amount(self, obj) -> int:
        return self._derived(obj)["net"]

    def get_components(self, obj) -> list:
        return self._derived(obj)["components"]


# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #

class BudgetLineSerializer(serializers.ModelSerializer):
    account = serializers.CharField(source="account.code", read_only=True)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)

    class Meta:
        model = BudgetLine
        fields = ["id", "account", "account_id", "cost_center", "period_no", "amount"]


class BudgetSerializer(serializers.ModelSerializer):
    fiscal_year = serializers.IntegerField(source="fiscal_year.year", read_only=True)
    is_locked = serializers.BooleanField(read_only=True)
    lines = BudgetLineSerializer(many=True, read_only=True)

    class Meta:
        model = Budget
        fields = [
            "id", "code", "name", "fiscal_year", "fiscal_year_id", "status",
            "is_locked", "approved_at", "lines",
        ]


# --------------------------------------------------------------------------- #
# Fixed assets                                                                #
# --------------------------------------------------------------------------- #

class DepreciationScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = DepreciationSchedule
        fields = ["id", "seq", "depreciation_date", "amount", "is_posted",
                  "journal_id", "posted_at"]


class FixedAssetSerializer(serializers.ModelSerializer):
    schedule = DepreciationScheduleSerializer(many=True, read_only=True)
    net_book_value = serializers.IntegerField(read_only=True)
    depreciable_base = serializers.IntegerField(read_only=True)
    cost_naira = serializers.SerializerMethodField()
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    method_display = serializers.CharField(source="get_method_display", read_only=True)

    class Meta:
        model = FixedAsset
        fields = [
            "id", "document_number", "name", "asset_code", "category", "category_display",
            "acquisition_date", "cost", "cost_naira", "salvage_value", "useful_life_months",
            "method", "method_display", "asset_status", "status", "accumulated_depreciation", "net_book_value",
            "depreciable_base", "acquisition_journal_id", "disposal_date",
            "disposal_journal_id", "schedule",
        ]

    def get_cost_naira(self, obj) -> str:
        return format_naira(obj.cost)


# --------------------------------------------------------------------------- #
# Audit log                                                                   #
# --------------------------------------------------------------------------- #

class FinanceAuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = FinanceAuditLog
        fields = [
            "id", "action", "status", "actor", "target_type", "target_id",
            "document_number", "message", "metadata", "created_at",
        ]
