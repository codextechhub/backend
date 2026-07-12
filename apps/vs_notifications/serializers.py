# =============================================================================
# vs_notifications / serializers.py
#
# Serializers:
#   NotificationListSerializer            — compact feed list (no body)
#   NotificationDetailSerializer          — full record including body/html_body
#   MarkReadSerializer                    — validates mark-read request payload
#   NotificationHistorySerializer         — admin history log list view
#   NotificationHistoryDetailSerializer   — admin history log detail view
#   NotificationEventTypeSerializer       — event type read (all users)
#   NotificationTemplateSerializer        — template management (Vision Staff)
#   NotificationTemplatePreviewSerializer — preview render (Vision Staff)
#   EffectiveSettingSerializer            — one row of the effective settings matrix
#   SettingsBulkUpdateSerializer          — bulk PATCH payload validator
#
# FLS: Notification.metadata is INTERNAL-ONLY and is never exposed by any
# serializer here. Do not add it.
# =============================================================================

from rest_framework import serializers

from .constants import ChannelChoices, NotificationErrorCode
from .exceptions import InvalidTemplateSyntaxError
from .models import (
    Notification,
    NotificationEventType,
    NotificationTemplate,
)
from .services.render import validate_template_syntax, render_notification_template


# ---------------------------------------------------------------------------
# Notification — feed (list)
# ---------------------------------------------------------------------------

class NotificationListSerializer(serializers.ModelSerializer):
    """
    Compact serializer for the in-app notification feed.
    Omits body for performance — clients fetch the detail endpoint on click.
    """
    event_type_key   = serializers.CharField(source="event_type.key",   read_only=True)
    event_type_label = serializers.CharField(source="event_type.label", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "event_type_key",
            "event_type_label",
            "channel",
            "subject",
            "is_read",
            "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Notification — detail
# ---------------------------------------------------------------------------

class NotificationDetailSerializer(serializers.ModelSerializer):
    """
    Full notification record including rendered body.
    Used for single-record retrieval (GET /notifications/{id}/).

    metadata is deliberately absent — it is internal-only (FLS).
    """
    event_type_key   = serializers.CharField(source="event_type.key",   read_only=True)
    event_type_label = serializers.CharField(source="event_type.label", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "event_type_key",
            "event_type_label",
            "channel",
            "subject",
            "body",
            "status",
            "is_read",
            "read_at",
            "dispatched_at",
            "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Mark-read request payload
# ---------------------------------------------------------------------------

class MarkReadSerializer(serializers.Serializer):
    """
    Validates the request body for POST /notifications/mark-read/.
    Accepts a list of notification UUIDs.
    """
    ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        min_length=1,
        max_length=100,
        help_text="List of notification UUIDs to mark as read. Maximum 100 per request.",
    )

    def validate_ids(self, value):
        # Guard: ensure all supplied IDs reference IN_APP notifications.
        # EMAIL notifications have no read state.
        if Notification.objects.filter(
            id__in=value,
            channel=ChannelChoices.EMAIL,
        ).exists():
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.READ_STATE_NOT_SUPPORTED_FOR_CHANNEL,
                    "message": "One or more supplied IDs reference email notifications. "
                               "Read state is only supported for in-app notifications.",
                }
            )
        return value


# ---------------------------------------------------------------------------
# Notification history — admin list
# ---------------------------------------------------------------------------

class NotificationHistorySerializer(serializers.ModelSerializer):
    """
    Compact history log serializer for admin list view.
    Body is excluded — fetch the detail endpoint for the full record.
    """
    event_type_key   = serializers.CharField(source="event_type.key",   read_only=True)
    event_type_label = serializers.CharField(source="event_type.label", read_only=True)
    recipient_name   = serializers.SerializerMethodField()
    recipient_email  = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "event_type_key",
            "event_type_label",
            "channel",
            "subject",
            "status",
            "retry_count",
            "failure_reason",
            "recipient_name",
            "recipient_email",
            "tenant",
            "dispatched_at",
            "created_at",
        ]
        read_only_fields = fields

    def get_recipient_name(self, obj):
        if obj.recipient_id:
            return getattr(obj.recipient, "get_full_name", lambda: "")() or obj.recipient.email
        return obj.unregistered_email

    def get_recipient_email(self, obj):
        return obj.effective_email


# ---------------------------------------------------------------------------
# Notification history — admin detail
# ---------------------------------------------------------------------------

class NotificationHistoryDetailSerializer(NotificationHistorySerializer):
    """
    Full history record including rendered body and failure details.
    metadata stays internal-only (FLS) — not exposed here.
    """
    class Meta(NotificationHistorySerializer.Meta):
        fields = NotificationHistorySerializer.Meta.fields + ["body"]


# ---------------------------------------------------------------------------
# Notification event type — read
# ---------------------------------------------------------------------------

class NotificationEventTypeSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for NotificationEventType.
    Accessible to all authenticated users (used by settings UI and template editor).
    """

    class Meta:
        model = NotificationEventType
        fields = [
            "id",
            "key",
            "label",
            "description",
            "source_module",
            "supported_channels",
            "default_enabled",
            "is_transactional",
            "is_active",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Notification template — Vision Staff management
# ---------------------------------------------------------------------------

class NotificationTemplateSerializer(serializers.ModelSerializer):
    """
    Full template serializer for Vision Staff create / read / update.
    Validates template syntax on save for subject, body, and html_body.
    """
    event_type_key = serializers.CharField(source="event_type.key", read_only=True)

    class Meta:
        model = NotificationTemplate
        fields = [
            "id",
            "event_type",
            "event_type_key",
            "channel",
            "subject",
            "body",
            "html_body",
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "event_type_key", "created_by", "updated_by",
            "created_at", "updated_at",
        ]

    def validate(self, data):
        def _field(name):
            return data.get(name, getattr(self.instance, name, "") if self.instance else "")

        body      = _field("body")
        subject   = _field("subject")
        html_body = _field("html_body")

        # Validate Django template syntax at save time for every content field.
        try:
            if body:
                validate_template_syntax(body, field="body")
            if subject:
                validate_template_syntax(subject, field="subject")
            if html_body:
                validate_template_syntax(html_body, field="html_body")
        except InvalidTemplateSyntaxError as exc:
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.INVALID_TEMPLATE_SYNTAX,
                    "message": exc.message,
                    "field": exc.field,
                }
            )
        return data

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        validated_data["updated_by"] = self.context["request"].user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data["updated_by"] = self.context["request"].user
        return super().update(instance, validated_data)


# ---------------------------------------------------------------------------
# Template preview — Vision Staff
# ---------------------------------------------------------------------------

class NotificationTemplatePreviewSerializer(serializers.Serializer):
    """
    Write-only serializer for POST /notifications/templates/{id}/preview/.
    Accepts a sample context dict, renders the template, and returns rendered
    subject, body, and html_body. Does not send any notification.
    """
    context = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        default=dict,
        help_text=(
            "Sample context dict for preview rendering. "
            "Keys should match the context variables defined for this event type."
        ),
    )

    def render(self, template_instance):
        """
        Render the template with the provided sample context.
        Returns {"subject": "...", "body": "...", "html_body": "..."}.
        """
        context = self.validated_data.get("context", {})
        try:
            rendered_subject, rendered_body, rendered_html = render_notification_template(
                template_instance, context
            )
        except Exception as exc:
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.INVALID_TEMPLATE_SYNTAX,
                    "message": f"Template rendering failed: {exc}",
                }
            )
        return {
            "subject": rendered_subject,
            "body": rendered_body,
            "html_body": rendered_html,
        }


# ---------------------------------------------------------------------------
# Effective settings matrix — read
# ---------------------------------------------------------------------------

class EffectiveSettingSerializer(serializers.Serializer):
    """
    One row of the EFFECTIVE settings matrix returned by GET .../settings/.

    Each row is one (event_type, channel) pair with its resolved value and the
    layer that produced it. Not backed by a model — the view builds plain dicts
    from resolve-layering, so this is a read-only shape validator/documenter.
    """
    event_type_key   = serializers.CharField()
    event_type_label = serializers.CharField()
    source_module    = serializers.CharField()
    channel          = serializers.CharField()
    is_enabled       = serializers.BooleanField()
    is_transactional = serializers.BooleanField()
    # Which layer produced is_enabled: "school", "platform", or "default".
    source           = serializers.CharField()


# ---------------------------------------------------------------------------
# Settings bulk update — PATCH .../settings/update/
# ---------------------------------------------------------------------------

class SettingUpdateItemSerializer(serializers.Serializer):
    """One override to upsert, addressed by (event_type_key, channel)."""
    event_type_key = serializers.CharField()
    channel        = serializers.CharField()
    is_enabled     = serializers.BooleanField()


class SettingsBulkUpdateSerializer(serializers.Serializer):
    """
    Validates the payload for PATCH .../settings/update/.

    Expected body:
        {
          "updates": [
            { "event_type_key": "billing.invoice_overdue", "channel": "email",
              "is_enabled": false }
          ]
        }

    Rows are addressed by (event_type_key, channel) and upserted, not updated by
    row id. All updates in a single request commit atomically. Business rules
    (unknown key/channel, unsupported channel, IN_APP disable, transactional
    toggle) are enforced in the view against the resolved event types.
    """
    updates = serializers.ListField(
        child=SettingUpdateItemSerializer(),
        allow_empty=False,
        min_length=1,
    )
