"""DRF serializers for the gateway records (read views + action responses)."""
from __future__ import annotations

from rest_framework import serializers

from vs_finance.money import format_naira
from vs_rbac.fls import FieldSecurityMixin

from .models import (
    CollectionIntent,
    PaymentEvent,
    PayoutBatch,
    PayoutInstruction,
    VirtualAccount,
    WebhookEvent,
)


class CollectionIntentSerializer(serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True, default=None)
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)
    deposit_account_code = serializers.CharField(source="deposit_account.code", read_only=True, default=None)
    deposit_account_name = serializers.CharField(source="deposit_account.name", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()
    payment_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = CollectionIntent
        fields = [
            "id", "entity_code", "provider", "channel", "reference", "provider_reference",
            "amount", "amount_naira", "status", "customer_code", "customer_name", "invoice_id",
            "deposit_account_code", "deposit_account_name",
            "payer_email", "payer_name", "narration", "checkout_url", "payment_id",
            "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)


class VirtualAccountSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True, default=None)
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)
    deposit_account_code = serializers.CharField(source="deposit_account.code", read_only=True, default=None)
    deposit_account_name = serializers.CharField(source="deposit_account.name", read_only=True, default=None)
    currency_code = serializers.CharField(source="currency.code", read_only=True, default=None)

    # FLS: the funding account number/name are only exposed to holders of the
    # sensitive grant; everyone else sees the record with these fields stripped.
    read_permissions = {
        "account_number": "payments.virtual_account.view_sensitive",
        "account_name": "payments.virtual_account.view_sensitive",
    }

    class Meta:
        model = VirtualAccount
        fields = [
            "id", "entity_code", "provider", "customer_code", "customer_name",
            "account_number", "bank_name", "account_name", "provider_reference",
            "deposit_account_code", "deposit_account_name", "currency_code",
            "status", "created_at",
        ]


class PayoutInstructionSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    entity_code = serializers.CharField(source="entity.code", read_only=True)
    amount_naira = serializers.SerializerMethodField()
    # WHT withheld on this line (carried on metadata) - net = amount − wht, and the
    # settlement journal credits a WHT payable for it.
    wht_amount = serializers.SerializerMethodField()
    # The bank/cash GL the booked payout credits - lets the console recap the
    # real settlement journal (Dr Accounts payable / Cr this account).
    source_account_code = serializers.CharField(source="source_account.code", read_only=True, default=None)
    source_account_name = serializers.CharField(source="source_account.name", read_only=True, default=None)

    # FLS: beneficiary bank details are PII - only holders of the sensitive grant
    # see them.
    read_permissions = {
        "beneficiary_name": "payments.payout.view_sensitive",
        "beneficiary_account_number": "payments.payout.view_sensitive",
        "beneficiary_bank_code": "payments.payout.view_sensitive",
    }

    class Meta:
        model = PayoutInstruction
        fields = [
            "id", "entity_code", "batch_id", "provider", "reference", "provider_reference",
            "amount", "amount_naira", "status", "beneficiary_name",
            "beneficiary_account_number", "beneficiary_bank_code", "narration",
            "source_account_code", "source_account_name", "wht_amount",
            "vendor_payment_id", "failure_reason", "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)

    def get_wht_amount(self, obj):
        return int((obj.metadata or {}).get("wht_amount", 0))


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


class PaymentEventSerializer(serializers.ModelSerializer):
    """Read serializer for the append-only gateway action log (transactions log)."""

    entity_code = serializers.CharField(source="entity.code", read_only=True, default=None)
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default=None)

    class Meta:
        model = PaymentEvent
        fields = [
            "id", "entity_code", "provider", "action", "action_display", "reference",
            "succeeded", "message", "metadata", "actor_email", "created_at",
        ]


class PayoutBatchSummarySerializer(serializers.ModelSerializer):
    """List view - omits the (potentially large) child instruction array."""

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


class WebhookEventSerializer(serializers.ModelSerializer):
    """Read serializer for an inbound provider webhook that needs an operator's eye.

    Deliberately omits ``payload``, ``raw_body``, ``headers`` and ``signature``. Those
    are stored verbatim for audit and replay and are the provider's own record: they
    carry signature material and whatever personal data the PSP chose to include, none
    of which a console list needs. What an operator needs is which event it was, what
    it was for, and why it did not go through.
    """

    amount = serializers.SerializerMethodField()
    amount_naira = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    target_reference = serializers.SerializerMethodField()
    target_kind = serializers.SerializerMethodField()

    class Meta:
        model = WebhookEvent
        fields = [
            "id", "provider", "event_type", "provider_reference", "status", "verified",
            "error", "created_at", "processed_at",
            "collection_id", "payout_id", "target_kind", "target_reference",
            "amount", "amount_naira", "customer_name",
        ]

    def _target(self, obj):
        return obj.collection or obj.payout

    def get_target_kind(self, obj) -> str | None:
        if obj.collection_id:
            return "COLLECTION"
        return "PAYOUT" if obj.payout_id else None

    def get_target_reference(self, obj) -> str | None:
        target = self._target(obj)
        return target.reference if target else None

    def get_amount(self, obj) -> int | None:
        target = self._target(obj)
        return int(target.amount) if target else None

    def get_amount_naira(self, obj) -> str | None:
        target = self._target(obj)
        return format_naira(target.amount) if target else None

    def get_customer_name(self, obj) -> str | None:
        """Who the money came from, when the event is a collection we could match."""
        customer = getattr(obj.collection, "customer", None) if obj.collection_id else None
        return customer.name if customer else None
