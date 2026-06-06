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
    FiscalPeriod,
    Invoice,
    JournalEntry,
    JournalLine,
    LedgerEntity,
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
