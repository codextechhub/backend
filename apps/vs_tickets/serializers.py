from __future__ import annotations

import os

from rest_framework import serializers

from core.storage import ALLOWED_EXTENSIONS
from vs_user.models import User

from .constants import CommentVisibility, TicketCategory, TicketPriority, TicketStatus
from .models import Ticket, TicketAttachment, TicketAuditLog, TicketComment
from .services.visibility import can_view_internal_notes

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class TicketUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "name", "email", "user_type", "role"]


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
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url


class TicketCommentSerializer(serializers.ModelSerializer):
    author = TicketUserSerializer(read_only=True)
    attachments = TicketAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = TicketComment
        fields = ["id", "author", "body", "visibility", "attachments", "created_at", "updated_at"]


class TicketSerializer(serializers.ModelSerializer):
    requester = TicketUserSerializer(read_only=True)
    assignee = TicketUserSerializer(read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True, default="")
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")
    comments_count = serializers.IntegerField(read_only=True)
    attachments_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Ticket
        fields = [
            "id", "ticket_number", "title", "description", "category", "priority",
            "status", "source", "requester", "assignee", "school", "school_name",
            "branch", "branch_name", "resolved_at", "closed_at", "comments_count",
            "attachments_count", "created_at", "updated_at",
        ]
        read_only_fields = [
            "ticket_number", "status", "source", "requester", "assignee",
            "resolved_at", "closed_at", "created_at", "updated_at",
        ]


class TicketDetailSerializer(TicketSerializer):
    comments = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()

    class Meta(TicketSerializer.Meta):
        fields = TicketSerializer.Meta.fields + ["comments", "attachments"]

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


class TicketCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=220)
    description = serializers.CharField()
    category = serializers.ChoiceField(choices=TicketCategory.choices, default=TicketCategory.SUPPORT)
    priority = serializers.ChoiceField(choices=TicketPriority.choices, default=TicketPriority.MEDIUM)


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
        # First-line validation; core.storage.DatabaseStorage re-checks both
        # rules as defense-in-depth but raises an unhandled 500 if hit.
        ext = os.path.splitext(value.name or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                f"File type '{ext or 'unknown'}' is not accepted — only "
                f"spreadsheets (csv/xlsx), images and PDFs."
            )
        if value.size > MAX_ATTACHMENT_BYTES:
            raise serializers.ValidationError("Attachments are limited to 10 MB.")
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
