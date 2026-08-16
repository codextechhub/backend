# =============================================================================
# vs_notifications / serializers.py
#
# Serializers:
#   NotificationListSerializer            - compact feed list (no body)
#   NotificationDetailSerializer          - full record including body/html_body
#   MarkReadSerializer                    - validates mark-read request payload
#   AcknowledgeRouteSerializer            - validates viewed application routes
#   NotificationHistorySerializer         - admin history log list view
#   NotificationHistoryDetailSerializer   - admin history log detail view
#   NotificationEventTypeSerializer       - event type read (all users)
#   NotificationTemplateSerializer        - template management (Vision Staff)
#   NotificationTemplatePreviewSerializer - preview render (Vision Staff)
#   EffectiveSettingSerializer            - one row of the effective settings matrix
#   SettingsBulkUpdateSerializer          - bulk PATCH payload validator
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
from .services.preview import sample_context, template_variables
from .services.render import validate_template_syntax, render_notification_template
from .services.routing import notification_action_url


# ---------------------------------------------------------------------------
# Notification - feed (list)
# ---------------------------------------------------------------------------

class NotificationPresentationMixin(serializers.ModelSerializer):
    subject = serializers.SerializerMethodField()
    action_url = serializers.SerializerMethodField()

    def get_subject(self, obj):
        return obj.subject.strip() or obj.event_type.label

    def get_action_url(self, obj):
        return notification_action_url(obj)


class NotificationListSerializer(NotificationPresentationMixin):
    """
    Compact serializer for the in-app notification feed.
    Includes the already-rendered plain-text body so the feed is useful without
    a second request or a detail drawer.
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
            "action_url",
            "is_read",
            "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Notification - detail
# ---------------------------------------------------------------------------

class NotificationDetailSerializer(NotificationPresentationMixin):
    """
    Full notification record including rendered body.
    Used for single-record retrieval (GET /notifications/{id}/).

    metadata is deliberately absent - it is internal-only (FLS).
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
            "action_url",
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


class AcknowledgeRouteSerializer(serializers.Serializer):
    path = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_path(self, value):
        if not value.startswith("/") or value.startswith("//") or "\\" in value:
            raise serializers.ValidationError("Path must be a local application route.")
        if "?" in value or "#" in value:
            raise serializers.ValidationError("Path must not include a query string or fragment.")
        return value.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# Notification history - admin list
# ---------------------------------------------------------------------------

class NotificationHistorySerializer(serializers.ModelSerializer):
    """
    Compact history log serializer for admin list view.
    Body is excluded - fetch the detail endpoint for the full record.
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
# Notification history - admin detail
# ---------------------------------------------------------------------------

class NotificationHistoryDetailSerializer(NotificationHistorySerializer):
    """
    Full history record including rendered body and failure details.
    metadata stays internal-only (FLS) - not exposed here.
    """
    class Meta(NotificationHistorySerializer.Meta):
        fields = NotificationHistorySerializer.Meta.fields + ["body"]


# ---------------------------------------------------------------------------
# Notification event type - read
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
# Notification template - Vision Staff management
# ---------------------------------------------------------------------------

class NotificationTemplateSerializer(serializers.ModelSerializer):
    """
    Full template serializer for Vision Staff create / read / update.

    A template is four plain fields - subject, body, cta_label, cta_url - and
    the read-only `variables` list tells the editor which {{ names }} the copy
    actually uses, so no separate variable documentation has to be kept in step.
    `uses_custom_html` flags the rare template that opted out of the shared
    email layout.

    Django template syntax is validated on save for every content field.
    """
    event_type_key   = serializers.CharField(source="event_type.key",   read_only=True)
    event_type_label = serializers.CharField(source="event_type.label", read_only=True)
    variables        = serializers.SerializerMethodField()

    class Meta:
        model = NotificationTemplate
        fields = [
            "id",
            "event_type",
            "event_type_key",
            "event_type_label",
            "channel",
            "subject",
            "body",
            "cta_label",
            "cta_url",
            "html_body",
            "html_is_custom",
            "variables",
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "event_type_key", "event_type_label", "variables",
            "created_by", "updated_by", "created_at", "updated_at",
        ]

    def get_variables(self, obj):
        return template_variables(
            obj.subject, obj.body, obj.cta_label, obj.cta_url, obj.html_body,
        )

    def validate(self, data):
        def _field(name):
            return data.get(name, getattr(self.instance, name, "") if self.instance else "")

        fields = {
            name: _field(name)
            for name in ("body", "subject", "html_body", "cta_label", "cta_url")
        }

        # Validate Django template syntax at save time for every content field.
        try:
            for name, value in fields.items():
                if value:
                    validate_template_syntax(value, field=name)
        except InvalidTemplateSyntaxError as exc:
            raise serializers.ValidationError(
                {
                    "error_code": NotificationErrorCode.INVALID_TEMPLATE_SYNTAX,
                    "message": exc.message,
                    "field": exc.field,
                }
            )

        # A label with nowhere to go renders nothing; say so instead of
        # silently dropping it at send time.
        if fields["cta_label"] and not fields["cta_url"]:
            raise serializers.ValidationError(
                {"cta_url": "Set a destination for the call-to-action, or clear cta_label."}
            )
        return data

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        validated_data["updated_by"] = self.context["request"].user
        return super().create(self._resolve_html_ownership(validated_data, None))

    def update(self, instance, validated_data):
        validated_data["updated_by"] = self.context["request"].user
        return super().update(instance, self._resolve_html_ownership(validated_data, instance))

    # Decide whether this write makes the markup hand-edited.
    @staticmethod
    def _resolve_html_ownership(validated_data, instance):
        """
        Sending html_body claims ownership of the markup; sending
        html_is_custom=False gives it back.

        Handing the caller an explicit flag AND inferring it from the payload
        looks redundant, but each covers a case the other cannot: an editor that
        simply saves whatever is in its HTML box would otherwise silently lose
        the edit on the next regeneration, and "reset this back to the standard
        design" has no other way to say so. An explicit flag always wins.
        """
        if "html_is_custom" in validated_data:
            if validated_data["html_is_custom"] is False:
                # A reset regenerates from the layout; drop any markup sent with it.
                validated_data.pop("html_body", None)
            return validated_data

        if "html_body" in validated_data:
            submitted = (validated_data["html_body"] or "").strip()
            # Compared against the standard for BOTH the old and the new
            # message text. An editor that posts its whole form back after the
            # user only touched the message would otherwise look like a hand
            # edit, and freeze that template on its previous wording forever.
            standards = set()
            if instance is not None:
                standards.add(instance.standard_html().strip())
            merged = NotificationTemplate(
                channel=validated_data.get(
                    "channel", getattr(instance, "channel", ChannelChoices.EMAIL),
                ),
                subject=validated_data.get("subject", getattr(instance, "subject", "")),
                body=validated_data.get("body", getattr(instance, "body", "")),
                cta_label=validated_data.get("cta_label", getattr(instance, "cta_label", "")),
                cta_url=validated_data.get("cta_url", getattr(instance, "cta_url", "")),
            )
            standards.add(merged.standard_html().strip())
            validated_data["html_is_custom"] = bool(submitted) and submitted not in standards
        return validated_data


# ---------------------------------------------------------------------------
# Template preview - Vision Staff
# ---------------------------------------------------------------------------

class DraftTemplateSerializer(serializers.Serializer):
    """Unsaved editor content. Every field optional; absent means "as stored"."""
    subject        = serializers.CharField(required=False, allow_blank=True, max_length=500)
    body           = serializers.CharField(required=False, allow_blank=True)
    cta_label      = serializers.CharField(required=False, allow_blank=True, max_length=80)
    cta_url        = serializers.CharField(required=False, allow_blank=True, max_length=500)
    html_body      = serializers.CharField(required=False, allow_blank=True)
    html_is_custom = serializers.BooleanField(required=False)


class NotificationTemplatePreviewSerializer(serializers.Serializer):
    """
    Serializer for GET/POST /notifications/templates/{id}/preview/.

    Renders the template and returns what a recipient would actually see: the
    subject, the plain body, and - for email - the finished HTML document,
    composed through the same shared layout dispatch uses. Nothing is sent and
    no Notification record is created.

    The sample context is filled in automatically from the variables the
    template uses, so a preview shows the real visual with no payload at all.
    Any context the caller does supply overrides the generated values.
    """
    context = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        default=dict,
        help_text=(
            "Optional sample values, keyed by context variable. Anything not "
            "supplied is filled with a representative sample value."
        ),
    )
    draft = DraftTemplateSerializer(
        required=False,
        help_text=(
            "Unsaved editor content to preview instead of the stored template. "
            "Nothing is written - this is how the console renders edits as they "
            "are typed."
        ),
    )

    def render(self, template_instance):
        """
        Render the template with sample context (caller values win).

        When the payload carries a draft, the preview is built from THAT rather
        than from the stored row, so the editor shows the change before anyone
        commits to it. The draft is applied to a detached copy; the database row
        is never touched.

        Returns the rendered message plus the context that produced it, so the
        editor can show which values were stand-ins.
        """
        template_instance = self._apply_draft(template_instance)
        variables = template_variables(
            template_instance.subject,
            template_instance.body,
            template_instance.cta_label,
            template_instance.cta_url,
            template_instance.html_body,
        )
        context = sample_context(
            variables, overrides=self.validated_data.get("context") or {},
        )
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
            "channel": template_instance.channel,
            "subject": rendered_subject,
            "body": rendered_body,
            # What the recipient sees: placeholders replaced with sample values.
            "html_body": rendered_html,
            # The markup that produced it, placeholders intact. This is what an
            # editor puts in its HTML box - showing the rendered version there
            # would quietly bake the sample data into the template on save.
            "html_source": template_instance.html_body,
            "html_is_custom": template_instance.html_is_custom,
            "variables": variables,
            "context_used": {key: str(value) for key, value in context.items()},
        }

    # Build a detached copy carrying the editor's unsaved values.
    def _apply_draft(self, template_instance):
        """
        Return the instance to preview: the stored row, or a copy of it with the
        draft applied. Never saved, and never mutates the passed-in instance.

        A draft that changes the message but leaves the markup alone gets the
        markup regenerated, so the preview shows what saving would actually
        produce rather than the previous wording inside the old HTML.
        """
        draft = self.validated_data.get("draft")
        if not draft:
            return template_instance

        copy = NotificationTemplate(
            id=template_instance.id,
            event_type_id=template_instance.event_type_id,
            channel=template_instance.channel,
            subject=draft.get("subject", template_instance.subject),
            body=draft.get("body", template_instance.body),
            cta_label=draft.get("cta_label", template_instance.cta_label),
            cta_url=draft.get("cta_url", template_instance.cta_url),
            html_is_custom=draft.get("html_is_custom", template_instance.html_is_custom),
        )
        copy.event_type = template_instance.event_type
        if copy.channel != ChannelChoices.EMAIL:
            copy.html_body = ""
        elif copy.html_is_custom:
            copy.html_body = draft.get("html_body", template_instance.html_body)
        else:
            copy.html_body = copy.standard_html()
        return copy


# ---------------------------------------------------------------------------
# Effective settings matrix - read
# ---------------------------------------------------------------------------

class EffectiveSettingSerializer(serializers.Serializer):
    """
    One row of the EFFECTIVE settings matrix returned by GET .../settings/.

    Each row is one (event_type, channel) pair with its resolved value and the
    layer that produced it. Not backed by a model - the view builds plain dicts
    from resolve-layering, so this is a read-only shape validator/documenter.
    """
    event_type_key   = serializers.CharField()
    event_type_label = serializers.CharField()
    source_module    = serializers.CharField()
    channel          = serializers.CharField()
    is_enabled       = serializers.BooleanField()
    is_transactional = serializers.BooleanField()
    # Which layer produced is_enabled: "tenant", "platform", or "default".
    source           = serializers.CharField()


# ---------------------------------------------------------------------------
# Settings bulk update - PATCH .../settings/update/
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
