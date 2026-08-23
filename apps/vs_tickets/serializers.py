from __future__ import annotations

from rest_framework import serializers
from rest_framework.reverse import reverse

from core.uploads import MAX_TICKET_ATTACHMENT_BYTES, TICKET_EXTENSIONS, validate_upload
from vs_user.models import User

from .constants import CommentVisibility, TicketCategory, TicketPriority, TicketStatus
from .context import allowed_keys, registered_choice_fields
from .models import Ticket, TicketAttachment, TicketAuditLog, TicketComment
from .services.visibility import can_view_internal_notes


class TicketUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)
    tenant_kind = serializers.CharField(source="tenant.kind", read_only=True)

    class Meta:
        model = User
        # ``tenant_kind`` replaces ``user_type``: on a ticket the distinction
        # that matters is support desk versus the tenant who raised it, and
        # that is the tenant's kind. ``role`` still says what the person does.
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
        # Read at build time rather than at import time: an app registers from
        # its own ready(), and this module is imported by URL loading, which
        # can happen first.
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
        # First-line validation through the shared checker, which also verifies the
        # bytes match the extension. core.storage.DatabaseStorage re-checks type and
        # size as defense-in-depth but raises an unhandled 500 if hit.
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
