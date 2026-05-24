# =============================================================================
# vs_notifications / serializers.py
#
# Serializers:
#   NotificationListSerializer          — compact feed list (no body)
#   NotificationDetailSerializer        — full record including body
#   MarkReadSerializer                  — validates mark-read request payload
#   NotificationHistorySerializer       — admin history log list view
#   NotificationHistoryDetailSerializer — admin history log detail view
#   NotificationEventTypeSerializer     — event type read (all users)
#   NotificationTemplateSerializer      — template management (Vision Staff)
#   NotificationTemplatePreviewSerializer — preview render (Vision Staff)
#   SchoolNotificationSettingSerializer — settings read + PATCH
#   SettingsBulkUpdateSerializer        — bulk PATCH payload validator
# =============================================================================

from django.utils import timezone
from rest_framework import serializers

from .constants import ChannelChoices, NotificationErrorCode
from .exceptions import (
    InvalidTemplateSyntaxError,
    InAppAlwaysEnabledError,
    ReadStateNotSupportedError,
)
from .models import (
    Notification,
    NotificationEventType,
    NotificationTemplate,
    NotificationStatus,
    SchoolNotificationSetting,
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
        from .constants import ChannelChoices
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
            "is_active",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Notification template — Vision Staff management
# ---------------------------------------------------------------------------

class NotificationTemplateSerializer(serializers.ModelSerializer):
    """
    Full template serializer for Vision Staff create / read / update.
    Validates template syntax on save for both subject and body.
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
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "event_type_key", "created_by", "updated_by", "created_at", "updated_at"]

    def validate(self, data):
        body    = data.get("body",    getattr(self.instance, "body",    "") if self.instance else "")
        subject = data.get("subject", getattr(self.instance, "subject", "") if self.instance else "")

        # Validate Django template syntax at save time
        try:
            if body:
                validate_template_syntax(body, field="body")
            if subject:
                validate_template_syntax(subject, field="subject")
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
    Accepts a sample context dict, renders the template, and returns
    rendered subject and body.  Does not send any notification.
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
        Returns {"subject": "...", "body": "..."}.
        """
        context = self.validated_data.get("context", {})
        try:
            rendered_subject, rendered_body = render_notification_template(
                template_instance, context
            )
        except Exception as exc:
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.INVALID_TEMPLATE_SYNTAX,
                    "message": f"Template rendering failed: {exc}",
                }
            )
        return {"subject": rendered_subject, "body": rendered_body}


# ---------------------------------------------------------------------------
# School notification setting — read + PATCH
# ---------------------------------------------------------------------------

class SchoolNotificationSettingSerializer(serializers.ModelSerializer):
    """
    Serializer for SchoolNotificationSetting.
    Used for the School Admin settings read and bulk PATCH endpoint.
    """
    event_type_key         = serializers.CharField(source="event_type.key",         read_only=True)
    event_type_label       = serializers.CharField(source="event_type.label",       read_only=True)
    event_type_description = serializers.CharField(source="event_type.description", read_only=True)
    event_type_module      = serializers.CharField(source="event_type.source_module", read_only=True)

    class Meta:
        model = SchoolNotificationSetting
        fields = [
            "id",
            "event_type",
            "event_type_key",
            "event_type_label",
            "event_type_description",
            "event_type_module",
            "channel",
            "is_enabled",
            "updated_at",
        ]
        read_only_fields = [
            "id", "event_type", "event_type_key", "event_type_label",
            "event_type_description", "event_type_module", "channel", "updated_at",
        ]

    def validate(self, data):
        is_enabled = data.get("is_enabled")
        instance   = self.instance

        # Guard: IN_APP channel cannot be disabled
        if (
            instance is not None
            and instance.channel == ChannelChoices.IN_APP
            and is_enabled is False
        ):
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.IN_APP_ALWAYS_ENABLED,
                    "message": "The in-app channel cannot be disabled for any event type.",
                }
            )
        return data


# ---------------------------------------------------------------------------
# Settings bulk update — PATCH /notifications/settings/
# ---------------------------------------------------------------------------

class SettingUpdateItemSerializer(serializers.Serializer):
    id         = serializers.UUIDField()
    is_enabled = serializers.BooleanField()


class SettingsBulkUpdateSerializer(serializers.Serializer):
    """
    Validates the payload for PATCH /notifications/settings/.

    Expected body:
        {
          "updates": [
            { "id": "uuid", "is_enabled": true },
            { "id": "uuid", "is_enabled": false }
          ]
        }

    All updates in a single request are committed atomically.
    """
    updates = serializers.ListField(
        child=SettingUpdateItemSerializer(),
        allow_empty=False,
        min_length=1,
    )

    def validate_updates(self, value):
        # Guard: ensure no IN_APP setting is being disabled
        in_app_disable_ids = []
        setting_ids = [item["id"] for item in value]

        settings_map = {
            str(s.id): s
            for s in SchoolNotificationSetting.objects.filter(id__in=setting_ids)
        }

        for item in value:
            setting = settings_map.get(str(item["id"]))
            if (
                setting is not None
                and setting.channel == ChannelChoices.IN_APP
                and item["is_enabled"] is False
            ):
                in_app_disable_ids.append(str(item["id"]))

        if in_app_disable_ids:
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.IN_APP_ALWAYS_ENABLED,
                    "message": (
                        "The in-app channel cannot be disabled. "
                        f"Affected setting IDs: {in_app_disable_ids}"
                    ),
                }
            )
        return value
