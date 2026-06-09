"""DRF serializers for the gateway records (read views + action responses)."""
from __future__ import annotations

from rest_framework import serializers

from vs_finance.money import format_naira
from vs_rbac.fls import FieldSecurityMixin

from .models import CollectionIntent, PayoutBatch, PayoutInstruction, VirtualAccount


class CollectionIntentSerializer(serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()
    payment_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = CollectionIntent
        fields = [
            "id", "entity_code", "provider", "channel", "reference", "provider_reference",
            "amount", "amount_naira", "status", "customer_code", "invoice_id",
            "payer_email", "payer_name", "narration", "checkout_url", "payment_id",
            "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)


class VirtualAccountSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True, default=None)

    # FLS: the funding account number/name are only exposed to holders of the
    # sensitive grant; everyone else sees the record with these fields stripped.
    read_permissions = {
        "account_number": "payments.virtual_account.view_sensitive",
        "account_name": "payments.virtual_account.view_sensitive",
    }

    class Meta:
        model = VirtualAccount
        fields = [
            "id", "entity_code", "provider", "customer_code", "account_number",
            "bank_name", "account_name", "provider_reference", "status", "created_at",
        ]


class PayoutInstructionSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    amount_naira = serializers.SerializerMethodField()

    # FLS: beneficiary bank details are PII — only holders of the sensitive grant
    # see them.
    read_permissions = {
        "beneficiary_name": "payments.payout.view_sensitive",
        "beneficiary_account_number": "payments.payout.view_sensitive",
    }

    class Meta:
        model = PayoutInstruction
        fields = [
            "id", "entity_code", "batch_id", "provider", "reference", "provider_reference",
            "amount", "amount_naira", "status", "beneficiary_name",
            "beneficiary_account_number", "beneficiary_bank_code", "narration",
            "vendor_payment_id", "failure_reason", "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)


class PayoutBatchSerializer(serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    total_amount_naira = serializers.SerializerMethodField()
    instructions = PayoutInstructionSerializer(many=True, read_only=True)

    class Meta:
        model = PayoutBatch
        fields = [
            "id", "entity_code", "provider", "reference", "title", "narration", "status",
            "total_amount", "total_amount_naira", "item_count", "submitted_at",
            "created_at", "instructions",
        ]

    def get_total_amount_naira(self, obj):
        return format_naira(obj.total_amount)


class PayoutBatchSummarySerializer(serializers.ModelSerializer):
    """List view — omits the (potentially large) child instruction array."""

    entity_code = serializers.CharField(source="entity.code", read_only=True)
    total_amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = PayoutBatch
        fields = [
            "id", "entity_code", "provider", "reference", "title", "status",
            "total_amount", "total_amount_naira", "item_count", "submitted_at",
            "created_at",
        ]

    def get_total_amount_naira(self, obj):
        return format_naira(obj.total_amount)
