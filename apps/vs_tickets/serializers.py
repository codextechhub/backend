from __future__ import annotations

from rest_framework import serializers
from rest_framework.reverse import reverse

from core.uploads import MAX_TICKET_ATTACHMENT_BYTES, TICKET_EXTENSIONS, validate_upload
from vs_user.models import User

from .constants import (
    CommentVisibility,
    GuideAnalyticsEventName,
    GuideAnalyticsOutcome,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from .context import allowed_keys, registered_choice_fields
from .models import Ticket, TicketAttachment, TicketAuditLog, TicketComment
from .services.visibility import can_view_internal_notes


class TicketUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)
    tenant_kind = serializers.CharField(source="tenant.kind", read_only=True)

    class Meta:
        model = User
        # On a ticket the distinction that matters is the support desk versus
        # the tenant who raised it, which is the tenant's kind.
        fields = ["id", "name", "email", "tenant_kind", "role"]


class TicketAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by = TicketUserSerializer(read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = TicketAttachment
        fields = [
            "id", "original_filename", "content_type", "size", "url",
            "uploaded_by", "comment_id", "created_at",
        ]

    def get_url(self, obj):
        if not obj.file:
            return ""
        request = self.context.get("request")
        return reverse(
            "ticket-attachment-download",
            kwargs={"pk": obj.ticket_id, "attachment_id": obj.pk},
            request=request,
        )


class TicketCommentSerializer(serializers.ModelSerializer):
    author = TicketUserSerializer(read_only=True)
    attachments = TicketAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = TicketComment
        fields = ["id", "author", "body", "visibility", "attachments", "created_at", "updated_at"]


class TicketSerializer(serializers.ModelSerializer):
    requester = TicketUserSerializer(read_only=True)
    assignee = TicketUserSerializer(read_only=True)
    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")
    comments_count = serializers.IntegerField(read_only=True)
    attachments_count = serializers.IntegerField(read_only=True)
    context = serializers.SerializerMethodField()

    def get_context(self, obj):
        # Read through the same allowlist the write path validates against, so
        # a key that stops being allowed also stops being returned on the rows
        # that already carry it.
        allowed = allowed_keys()
        return {key: value for key, value in (obj.context or {}).items() if key in allowed}

    class Meta:
        model = Ticket
        fields = [
            "id", "ticket_number", "title", "description", "category", "priority",
            "status", "source", "context", "requester", "assignee", "tenant",
            "branch", "branch_name", "resolved_at", "closed_at", "comments_count",
            "attachments_count", "created_at", "updated_at",
        ]
        read_only_fields = [
            "ticket_number", "status", "source", "context", "requester", "assignee",
            "resolved_at", "closed_at", "created_at", "updated_at",
        ]


class TicketDetailSerializer(TicketSerializer):
    comments = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    class Meta(TicketSerializer.Meta):
        fields = TicketSerializer.Meta.fields + [
            "comments", "attachments", "capabilities", "is_following",
        ]

    def _sees_internal(self, obj) -> bool:
        if not hasattr(self, "_sees_internal_cache"):
            user = self.context["request"].user
            self._sees_internal_cache = can_view_internal_notes(user, obj)
        return self._sees_internal_cache

    def get_comments(self, obj):
        comments = obj.comments.select_related("author").prefetch_related("attachments")
        if not self._sees_internal(obj):
            comments = comments.filter(visibility=CommentVisibility.PUBLIC)
        return TicketCommentSerializer(comments, many=True, context=self.context).data

    def get_attachments(self, obj):
        attachments = obj.attachments.select_related("uploaded_by")
        if not self._sees_internal(obj):
            # Files hanging off internal notes must stay as hidden as the notes.
            attachments = attachments.exclude(comment__visibility=CommentVisibility.INTERNAL)
        return TicketAttachmentSerializer(attachments, many=True, context=self.context).data

    def get_capabilities(self, obj):
        from .services.visibility import (
            can_attach_to_ticket,
            can_comment_on_ticket,
            can_update_ticket_fields,
        )

        user = self.context["request"].user
        return {
            "can_comment": can_comment_on_ticket(user, obj),
            "can_attach": can_attach_to_ticket(user, obj),
            "can_update": can_update_ticket_fields(user, obj),
        }

    def get_is_following(self, obj):
        from .services.subscriptions import is_following

        return is_following(obj, self.context["request"].user)


class TicketContextSerializer(serializers.Serializer):
    """The allowlist, enforced.

    The four fields below are this app's own. Anything a module registered
    through :mod:`vs_tickets.context` is added per instance in ``__init__``,
    always as a ``ChoiceField`` over a closed vocabulary, so a module can widen
    what a ticket carries without this app importing it and without loosening
    what any value may be.
    """

    guide_id = serializers.RegexField(r"^[a-z0-9][a-z0-9.-]{0,119}$", required=False)
    route_pattern = serializers.RegexField(
        r"^/[a-z0-9_./:-]{0,199}$",
        required=False,
    )
    product_area = serializers.ChoiceField(choices=[
        "Account", "Audit and security", "Console", "Data imports", "Exports",
        "Finance", "Health", "Notifications", "Onboarding", "Organogram",
        "Permissions", "Platform health", "Procurement", "Roles",
        "School management", "Settings", "Support", "Tasks", "Users", "Workflow",
    ], required=False)
    app_version = serializers.RegexField(r"^[A-Za-z0-9._+-]{1,40}$", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Read at build time, not import time: URL loading can beat an app's
        # own ready() to it.
        for key, choices in registered_choice_fields().items():
            self.fields[key] = serializers.ChoiceField(
                choices=list(choices), required=False,
            )

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Context must be an object.")
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({
                key: ["This context field is not allowed."] for key in unknown
            })
        return super().to_internal_value(data)

    def validate_route_pattern(self, value):
        # Parameter placeholders prove that record identifiers were removed.
        # Reject query strings and fragments even if a future regex is relaxed.
        if "?" in value or "#" in value or any(character.isdigit() for character in value):
            raise serializers.ValidationError("Use a normalized route pattern without query or fragment data.")
        return value


class TicketCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=220)
    description = serializers.CharField()
    category = serializers.ChoiceField(choices=TicketCategory.choices, default=TicketCategory.SUPPORT)
    priority = serializers.ChoiceField(choices=TicketPriority.choices, default=TicketPriority.MEDIUM)
    context = TicketContextSerializer(required=False, default=dict)


class TicketUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=220, required=False)
    description = serializers.CharField(required=False)
    category = serializers.ChoiceField(choices=TicketCategory.choices, required=False)
    priority = serializers.ChoiceField(choices=TicketPriority.choices, required=False)


class TicketAssignSerializer(serializers.Serializer):
    assignee_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_assignee_id(self, value):
        if value is None:
            return value
        if not User.objects.filter(pk=value).exists():
            raise serializers.ValidationError("No such user.")
        return value


class TicketTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=TicketStatus.choices)


class TicketCommentCreateSerializer(serializers.Serializer):
    body = serializers.CharField()
    visibility = serializers.ChoiceField(choices=CommentVisibility.choices, default=CommentVisibility.PUBLIC)


class TicketAttachmentCreateSerializer(serializers.Serializer):
    file = serializers.FileField()
    comment_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_file(self, value):
        """Reject the file here rather than letting storage reject it later.

        ``core.storage.DatabaseStorage`` re-checks type and size as
        defence-in-depth, but it raises an unhandled 500 when it does, so this
        has to be the check that actually refuses. The shared checker also
        verifies that the bytes match the extension.
        """
        validate_upload(
            value,
            allowed=TICKET_EXTENSIONS,
            max_bytes=MAX_TICKET_ATTACHMENT_BYTES,
            size_message="Attachments are limited to 10 MB.",
            type_message="File type is not accepted - only spreadsheets "
                         "(csv/xls/xlsx), images and PDFs.",
        )
        return value


class TicketAuditLogSerializer(serializers.ModelSerializer):
    actor = TicketUserSerializer(read_only=True)

    class Meta:
        model = TicketAuditLog
        fields = [
            "id", "actor", "action", "summary", "before_data", "after_data",
            "metadata", "created_at",
        ]


class TicketDashboardSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    by_status = serializers.DictField(child=serializers.IntegerField())
    by_priority = serializers.DictField(child=serializers.IntegerField())
    by_category = serializers.DictField(child=serializers.IntegerField())
    assigned_to_me = serializers.IntegerField()
    requested_by_me = serializers.IntegerField()


class GuideAnalyticsEventSerializer(serializers.Serializer):
    """Closed browser event contract with no arbitrary metadata channel."""

    name = serializers.ChoiceField(choices=GuideAnalyticsEventName.choices)
    guide_id = serializers.RegexField(
        r"^[a-z0-9][a-z0-9.-]{0,119}$",
        required=False,
        allow_blank=True,
    )
    walkthrough_id = serializers.RegexField(
        r"^[a-z0-9][a-z0-9.-]{0,139}$",
        required=False,
        allow_blank=True,
    )
    step_id = serializers.RegexField(
        r"^[a-z0-9][a-z0-9-]{0,99}$",
        required=False,
        allow_blank=True,
    )
    outcome = serializers.ChoiceField(
        choices=GuideAnalyticsOutcome.choices,
        required=False,
        allow_blank=True,
    )
    query = serializers.CharField(required=False, allow_blank=True, max_length=160)
    route_pattern = serializers.RegexField(
        r"^/[a-z0-9_./:-]{0,199}$",
        required=False,
        allow_blank=True,
    )
    result_count = serializers.IntegerField(required=False, min_value=0, max_value=0)

    EVENT_FIELDS = {
        GuideAnalyticsEventName.GUIDE_VIEWED: {"name", "guide_id"},
        GuideAnalyticsEventName.GUIDE_COMPLETED: {"name", "guide_id"},
        GuideAnalyticsEventName.HELPFUL_VOTED: {"name", "guide_id", "outcome"},
        GuideAnalyticsEventName.OUTDATED_REPORTED: {"name", "guide_id"},
        GuideAnalyticsEventName.WALKTHROUGH_EXITED: {
            "name", "guide_id", "walkthrough_id", "step_id", "outcome",
        },
        GuideAnalyticsEventName.SEARCH_NO_RESULTS: {
            "name", "query", "route_pattern", "result_count",
        },
    }

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Analytics event must be an object.")
        name = data.get("name")
        allowed = self.EVENT_FIELDS.get(name, {"name"})
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise serializers.ValidationError({
                key: ["This analytics field is not allowed for the event."]
                for key in unknown
            })
        return super().to_internal_value(data)

    def validate_route_pattern(self, value):
        if "?" in value or "#" in value or any(character.isdigit() for character in value):
            raise serializers.ValidationError(
                "Use a normalized route pattern without record, query, or fragment data."
            )
        return value

    def validate(self, attrs):
        name = attrs["name"]
        if name == GuideAnalyticsEventName.SEARCH_NO_RESULTS:
            if not attrs.get("query", "").strip():
                raise serializers.ValidationError({"query": "A no-result search needs a query."})
            attrs.setdefault("result_count", 0)
            return attrs

        if not attrs.get("guide_id"):
            raise serializers.ValidationError({"guide_id": "This event needs a guide id."})
        if name == GuideAnalyticsEventName.HELPFUL_VOTED and attrs.get("outcome") not in {
            GuideAnalyticsOutcome.HELPFUL,
            GuideAnalyticsOutcome.NOT_HELPFUL,
        }:
            raise serializers.ValidationError({"outcome": "Choose helpful or not_helpful."})
        if name == GuideAnalyticsEventName.WALKTHROUGH_EXITED:
            missing = [key for key in ("walkthrough_id", "step_id", "outcome") if not attrs.get(key)]
            if missing:
                raise serializers.ValidationError({key: "This field is required." for key in missing})
            if attrs["outcome"] not in {
                GuideAnalyticsOutcome.FINISHED,
                GuideAnalyticsOutcome.PAUSED,
                GuideAnalyticsOutcome.TARGET_UNAVAILABLE,
            }:
                raise serializers.ValidationError({"outcome": "Invalid walkthrough exit outcome."})
        return attrs
