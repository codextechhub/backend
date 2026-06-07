"""DRF serializers for the gateway records (read views + action responses)."""
from __future__ import annotations

from rest_framework import serializers

from vs_finance.money import format_naira

from .models import CollectionIntent, PayoutInstruction, VirtualAccount


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


class VirtualAccountSerializer(serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True, default=None)

    class Meta:
        model = VirtualAccount
        fields = [
            "id", "entity_code", "provider", "customer_code", "account_number",
            "bank_name", "account_name", "provider_reference", "status", "created_at",
        ]


class PayoutInstructionSerializer(serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = PayoutInstruction
        fields = [
            "id", "entity_code", "provider", "reference", "provider_reference", "amount",
            "amount_naira", "status", "beneficiary_name", "beneficiary_account_number",
            "beneficiary_bank_code", "narration", "vendor_payment_id", "failure_reason",
            "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)
