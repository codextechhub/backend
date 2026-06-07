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

from .models import (
    Account,
    BankAccount,
    BankStatementLine,
    Budget,
    BudgetLine,
    Concession,
    CostCenter,
    CreditNote,
    CreditNoteLine,
    Currency,
    Dimension,
    DepreciationSchedule,
    DunningNotice,
    DunningPolicy,
    DunningStage,
    ExpenseClaim,
    ExpenseClaimLine,
    FinanceAuditLog,
    FixedAsset,
    FiscalPeriod,
    FxRate,
    Invoice,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    PaymentPlan,
    PaymentPlanInstallment,
    PayrollLine,
    PayrollRun,
    Refund,
    TaxCode,
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


class AccountSerializer(serializers.ModelSerializer):
    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)

    class Meta:
        model = Account
        fields = [
            "id", "code", "name", "account_type", "normal_balance",
            "is_contra", "is_postable", "is_active", "parent_id", "parent_code",
        ]


class FiscalPeriodSerializer(serializers.ModelSerializer):
    fiscal_year = serializers.IntegerField(source="fiscal_year.year", read_only=True)

    class Meta:
        model = FiscalPeriod
        fields = [
            "id", "period_no", "name", "fiscal_year",
            "start_date", "end_date", "status", "closed_at",
        ]


class JournalLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    debit_naira = serializers.SerializerMethodField()
    credit_naira = serializers.SerializerMethodField()

    class Meta:
        model = JournalLine
        fields = [
            "id", "line_no", "account_id", "account_code", "account_name",
            "debit", "credit", "debit_naira", "credit_naira", "description",
        ]

    def get_debit_naira(self, obj) -> str:
        return format_naira(obj.debit)

    def get_credit_naira(self, obj) -> str:
        return format_naira(obj.credit)


class JournalEntryListSerializer(serializers.ModelSerializer):
    period = serializers.CharField(source="period.name", read_only=True, default=None)

    class Meta:
        model = JournalEntry
        fields = [
            "id", "document_number", "date", "period", "source",
            "status", "narration", "reference", "posted_at",
        ]


class JournalEntryDetailSerializer(JournalEntryListSerializer):
    lines = JournalLineSerializer(many=True, read_only=True)
    total_debit = serializers.SerializerMethodField()
    total_credit = serializers.SerializerMethodField()

    class Meta(JournalEntryListSerializer.Meta):
        fields = JournalEntryListSerializer.Meta.fields + [
            "lines", "total_debit", "total_credit", "reverses_id",
        ]

    def _totals(self, obj):
        cache = getattr(self, "_totals_cache", {})
        if obj.id not in cache:
            cache[obj.id] = obj.totals()
            self._totals_cache = cache
        return cache[obj.id]

    def get_total_debit(self, obj) -> int:
        return self._totals(obj)[0]

    def get_total_credit(self, obj) -> int:
        return self._totals(obj)[1]


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
    unallocated_amount = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    lines = CreditNoteLineSerializer(many=True, read_only=True)

    class Meta:
        model = CreditNote
        fields = [
            "id", "document_number", "kind", "customer_id", "customer_code",
            "customer_name", "invoice_id", "note_date", "status",
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

class BankAccountSerializer(serializers.ModelSerializer):
    gl_account = serializers.CharField(source="gl_account.code", read_only=True)
    currency = serializers.CharField(source="currency_id", read_only=True, default=None)

    class Meta:
        model = BankAccount
        fields = [
            "id", "name", "bank_name", "account_number",
            "gl_account", "gl_account_id", "currency", "is_active",
        ]


class BankStatementLineSerializer(serializers.ModelSerializer):
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = BankStatementLine
        fields = [
            "id", "bank_account_id", "txn_date", "description", "reference",
            "amount", "amount_naira", "status", "matched_line_id",
            "adjusting_journal_id", "external_id", "reconciled_at",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

class ExpenseClaimLineSerializer(serializers.ModelSerializer):
    expense_account = serializers.CharField(source="expense_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    line_total = serializers.IntegerField(read_only=True)

    class Meta:
        model = ExpenseClaimLine
        fields = [
            "id", "line_no", "description", "expense_account", "quantity",
            "unit_price", "tax_code", "net_amount", "tax_amount", "line_total",
            "cost_center",
        ]


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
# Payroll                                                                     #
# --------------------------------------------------------------------------- #

class PayrollLineSerializer(serializers.ModelSerializer):
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)

    class Meta:
        model = PayrollLine
        fields = [
            "id", "line_no", "employee_id", "employee_name",
            "gross_amount", "paye_amount", "pension_amount", "net_amount",
            "cost_center",
        ]


class PayrollRunSerializer(serializers.ModelSerializer):
    lines = PayrollLineSerializer(many=True, read_only=True)
    net_total_naira = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRun
        fields = [
            "id", "document_number", "pay_date", "period_label", "narration",
            "run_status", "status", "gross_total", "paye_total", "pension_total",
            "net_total", "net_total_naira", "bank_account_id",
            "journal_id", "disbursement_journal_id", "lines",
        ]

    def get_net_total_naira(self, obj) -> str:
        return format_naira(obj.net_total)


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
            "id", "name", "fiscal_year", "fiscal_year_id", "status",
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

    class Meta:
        model = FixedAsset
        fields = [
            "id", "document_number", "name", "asset_code", "acquisition_date",
            "cost", "cost_naira", "salvage_value", "useful_life_months", "method",
            "asset_status", "status", "accumulated_depreciation", "net_book_value",
            "depreciable_base", "acquisition_journal_id", "schedule",
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
