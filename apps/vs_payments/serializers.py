"""DRF serializers for the gateway records (read views + action responses)."""
from __future__ import annotations

import re

from rest_framework import serializers

from vs_finance.money import format_naira
from vs_finance.serializers import ApprovalStateMixin
from vs_rbac.field_enforcement import FieldAccessMixin, can_read

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
    branch = serializers.IntegerField(source="branch_id", read_only=True)
    settlement_entry_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = CollectionIntent
        fields = [
            "id", "entity_code", "branch", "provider", "channel", "reference", "provider_reference",
            "amount", "amount_naira", "status", "customer_code", "customer_name", "invoice_id",
            "deposit_account_code", "deposit_account_name",
            "payer_email", "payer_name", "narration", "checkout_url", "payment_id",
            "fee", "settlement_entry_id", "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)


class VirtualAccountSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One provider-issued collection account.

    The number and the name on it are the registered fields of
    ``payments.virtual_account``. Neither is writable: the provider issues
    both when the account is opened, which is also why the payload names no
    read-only fields. A resource a client can never write has nothing to grey.
    """

    field_resource = "payments.virtual_account"

    entity_code = serializers.CharField(source="entity.code", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True, default=None)
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)
    deposit_account_code = serializers.CharField(source="deposit_account.code", read_only=True, default=None)
    deposit_account_name = serializers.CharField(source="deposit_account.name", read_only=True, default=None)
    currency_code = serializers.CharField(source="currency.code", read_only=True, default=None)
    branch = serializers.IntegerField(source="branch_id", read_only=True)

    class Meta:
        model = VirtualAccount
        fields = [
            "id", "entity_code", "branch", "provider", "customer_code", "customer_name",
            "account_number", "bank_name", "account_name", "provider_reference",
            "deposit_account_code", "deposit_account_name", "currency_code",
            "status", "created_at",
        ]


class PayoutInstructionSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One outgoing payment, with the beneficiary behind its own switches.

    The beneficiary's name, account number and bank code are the registered
    fields of ``payments.payout``. None is writable: they are copied from the
    verified vendor record, and a value a caller supplies is only ever
    compared against it. The payload therefore names no read-only fields, a
    resource a client can never write having nothing to grey.
    """

    field_resource = "payments.payout"

    entity_code = serializers.CharField(source="entity.code", read_only=True)
    amount_naira = serializers.SerializerMethodField()
    # WHT withheld on this line (carried on metadata) - net = amount − wht, and the
    # settlement journal credits a WHT payable for it.
    wht_amount = serializers.SerializerMethodField()
    # The bank/cash GL the booked payout credits - lets the console recap the
    # real settlement journal (Dr Accounts payable / Cr this account).
    source_account_code = serializers.CharField(source="source_account.code", read_only=True, default=None)
    source_account_name = serializers.CharField(source="source_account.name", read_only=True, default=None)
    branch = serializers.IntegerField(source="branch_id", read_only=True)

    class Meta:
        model = PayoutInstruction
        fields = [
            "id", "entity_code", "branch", "batch_id", "provider", "reference", "provider_reference",
            "amount", "amount_naira", "status", "beneficiary_name",
            "beneficiary_account_number", "beneficiary_bank_code", "narration",
            "source_account_code", "source_account_name", "wht_amount",
            "vendor_payment_id", "failure_reason", "confirmed_at", "created_at",
        ]

    def get_amount_naira(self, obj):
        return format_naira(obj.amount)

    def get_wht_amount(self, obj):
        return int((obj.metadata or {}).get("wht_amount", 0))


class PayoutBatchSerializer(ApprovalStateMixin, serializers.ModelSerializer):
    """A payout batch with its instructions.

    ``approval_state`` and ``approval_returned`` say where it stands with its
    approval route (:class:`vs_finance.serializers.ApprovalStateMixin`): a batch
    an approver handed back reads PENDING and returned while it stays DRAFT, and
    is resumed from the approvals screen with the detail's ``workflow_instance_id``.
    """

    entity_code = serializers.CharField(source="entity.code", read_only=True)
    total_amount_naira = serializers.SerializerMethodField()
    instructions = PayoutInstructionSerializer(many=True, read_only=True)
    branch = serializers.IntegerField(source="branch_id", read_only=True)

    class Meta:
        model = PayoutBatch
        fields = [
            "id", "entity_code", "branch", "provider", "reference", "title", "narration", "status",
            "total_amount", "total_amount_naira", "item_count", "submitted_at",
            "created_at", "instructions", "approval_state", "approval_returned",
        ]

    def get_total_amount_naira(self, obj):
        return format_naira(obj.total_amount)


#: The ``metadata`` keys of a gateway action a reader of the log is shown.
#:
#: The column is free-form JSON that every gateway action writes into, so what it
#: holds is whatever a writer put there, today or in a row written years ago:
#: internal record ids, per-line breakdowns, and anything a later writer adds. The
#: log shows a named set of figures and codes that explain the action on its own
#: row, and never the column itself.
EVENT_METADATA_FIELDS = (
    "channel",  # How a collection was taken (checkout, virtual account).
    "error_code",  # Why a rejected action was refused.
    "gross_amount", "wht_amount", "transfer_amount", "wht_source",  # A payout's money.
    "submitted", "failed",  # A batch submission's outcome counts.
    "overturned_status",  # The no-money answer a confirmed collection overturned.
    "paid_on", "booked_on", "booked_late_reason",  # When money moved and was booked.
    "gross", "fee", "net",  # A settlement's payments, the provider's fees and what arrived.
)

#: Actions whose message names a virtual account by its number.
_VIRTUAL_ACCOUNT_ACTIONS = ("VIRTUAL_ACCOUNT_CREATED", "VIRTUAL_ACCOUNT_STATUS_CHANGED")
_VIRTUAL_ACCOUNT_NUMBER = re.compile(r"^Virtual account \S+ ")


class PaymentEventPeopleListSerializer(serializers.ListSerializer):
    """Resolve the named people for one transaction-log page in bulk."""

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        rows = list(data)
        prime_exit_states(self.context, (
            user_id for row in rows
            for user_id in (row.actor_user_id, row.proxied_by_id)
        ))
        return super().to_representation(rows)


class PaymentEventSerializer(serializers.ModelSerializer):
    """Read serializer for the append-only gateway action log (transactions log).

    ``metadata`` carries only :data:`EVENT_METADATA_FIELDS`, and only scalar values
    of them. A virtual account's actions name its account number in the message;
    for a caller whose roles hide ``payments.virtual_account.account_number`` the
    number is left out of the message, as it is absent from the account's own
    record, so the log says no more than the record would.

    ``actor_email`` is the person in whose name the action ran. Each named
    person has a separate employment-exit flag. An action taken under a proxy
    also says who really did it: ``real_actor_name``,
    ``proxied_user_name`` and the ready ``acted_label`` ("Ada Obi for Chioma
    Okafor") come from :mod:`core.attribution`.
    """

    entity_code = serializers.CharField(source="entity.code", read_only=True, default=None)
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    actor_email = serializers.CharField(source="actor_user.email", read_only=True, default=None)
    message = serializers.SerializerMethodField()
    metadata = serializers.SerializerMethodField()
    real_actor_name = serializers.SerializerMethodField()
    proxied_user_name = serializers.SerializerMethodField()
    acted_label = serializers.SerializerMethodField()
    actor_user_is_exited = serializers.SerializerMethodField()
    proxied_by_is_exited = serializers.SerializerMethodField()

    class Meta:
        model = PaymentEvent
        list_serializer_class = PaymentEventPeopleListSerializer
        fields = [
            "id", "entity_code", "provider", "action", "action_display", "reference",
            "succeeded", "message", "metadata", "actor_email", "created_at",
            "real_actor_name", "proxied_user_name", "acted_label",
            "actor_user_is_exited", "proxied_by_is_exited",
        ]

    def to_representation(self, obj):
        from core.person_exit import prime_exit_states

        prime_exit_states(self.context, (obj.actor_user_id, obj.proxied_by_id))
        return super().to_representation(obj)

    def get_actor_user_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.actor_user_id)

    def get_proxied_by_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.proxied_by_id)

    def get_message(self, obj):
        message = obj.message or ""
        if obj.action in _VIRTUAL_ACCOUNT_ACTIONS and not self._reads_account_numbers():
            return _VIRTUAL_ACCOUNT_NUMBER.sub("Virtual account ", message)
        return message

    def get_metadata(self, obj):
        stored = obj.metadata if isinstance(obj.metadata, dict) else {}
        return {
            key: stored[key] for key in EVENT_METADATA_FIELDS
            if key in stored and isinstance(stored[key], (str, int, float, bool))
        }

    def _reads_account_numbers(self) -> bool:
        """Evaluated once per page; the access map behind it is cached on the request."""
        if not hasattr(self, "_account_numbers_readable"):
            self._account_numbers_readable = can_read(
                self.context.get("request"), "payments.virtual_account.account_number")
        return self._account_numbers_readable

    def _attribution(self, obj) -> dict:
        from core.attribution import proxy_attribution

        actor = obj.actor_user if obj.actor_user_id else None
        if obj.proxied_by_id:
            return proxy_attribution(obj.proxied_by, actor)
        return proxy_attribution(actor)

    def get_real_actor_name(self, obj) -> str | None:
        return self._attribution(obj)["real_actor_name"]

    def get_proxied_user_name(self, obj) -> str | None:
        return self._attribution(obj)["proxied_user_name"]

    def get_acted_label(self, obj) -> str:
        return self._attribution(obj)["acted_label"]


class PayoutBatchSummarySerializer(ApprovalStateMixin, serializers.ModelSerializer):
    """List view - omits the (potentially large) child instruction array.

    Carries ``approval_state`` and ``approval_returned``, read once per page.
    """

    entity_code = serializers.CharField(source="entity.code", read_only=True)
    total_amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = PayoutBatch
        fields = [
            "id", "entity_code", "provider", "reference", "title", "status",
            "total_amount", "total_amount_naira", "item_count", "submitted_at",
            "created_at", "approval_state", "approval_returned",
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
