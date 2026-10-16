"""Shapes for the staff notice screens, and for the pupil portal's feed."""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin

from .models import Notice
from .services import notices as notice_service


def _name(user) -> str:
    return user.full_name if user is not None else ""


class NoticeSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A notice on a staff screen.

    ``read_count`` and ``audience_count`` are filled only on the detail, where
    ``context["with_reads"]`` asks for them, because counting an audience is a
    query per notice that a list of fifty should not make. ``can_manage`` says
    whether the caller may correct, publish or withdraw this notice.
    """

    field_resource = "school.notices"
    field_access_detail = True

    status_label = serializers.CharField(source="get_status_display", read_only=True)
    audience = serializers.SerializerMethodField()
    audience_label = serializers.SerializerMethodField()
    published_by = serializers.SerializerMethodField()
    withdrawn_by = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    read_count = serializers.SerializerMethodField()
    audience_count = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Notice
        fields = [
            "id", "title", "body", "status", "status_label",
            "audience", "audience_label",
            "created_by", "created_at",
            "published_at", "published_by", "edited_at",
            "withdrawn_at", "withdrawn_by",
            "read_count", "audience_count", "can_manage",
        ]
        read_only_fields = fields

    def get_audience(self, obj):
        return notice_service.audience_payload(obj)

    def get_audience_label(self, obj):
        return notice_service.audience_label(obj)

    def get_published_by(self, obj):
        return _name(obj.published_by)

    def get_withdrawn_by(self, obj):
        return _name(obj.withdrawn_by)

    def get_created_by(self, obj):
        return _name(obj.created_by)

    def _counts(self, obj):
        if not self.context.get("with_reads") or obj.status == "DRAFT":
            return None
        cache = self.context.setdefault("_read_counts", {})
        if obj.pk not in cache:
            cache[obj.pk] = notice_service.read_counts(obj)
        return cache[obj.pk]

    def get_read_count(self, obj):
        counts = self._counts(obj)
        return counts["read"] if counts else None

    def get_audience_count(self, obj):
        counts = self._counts(obj)
        return counts["audience"] if counts else None

    def get_can_manage(self, obj):
        reach = self.context.get("reach")
        request = self.context.get("request")
        if reach is None or request is None or obj.status == "WITHDRAWN":
            return False
        return notice_service.may_manage(reach, obj, request.user)


class AudienceEntrySerializer(serializers.Serializer):
    kind = serializers.CharField()
    id = serializers.IntegerField(required=False, allow_null=True)


class NoticeWriteSerializer(FieldAccessMixin, serializers.Serializer):
    """What a poster sends to draft or correct a notice."""

    field_resource = "school.notices"

    title = serializers.CharField(required=False, allow_blank=True)
    body = serializers.CharField(required=False, allow_blank=True)
    audience = AudienceEntrySerializer(many=True, required=False)
    publish = serializers.BooleanField(required=False, default=False)


class PortalNoticeSerializer(serializers.ModelSerializer):
    """A notice in a pupil's feed: what it says, who it is for, whether they read it.

    Carries nothing about who posted it or how many others opened it.
    """

    audience_label = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()

    class Meta:
        model = Notice
        fields = ["id", "title", "body", "audience_label", "published_at", "edited_at", "unread"]
        read_only_fields = fields

    def get_audience_label(self, obj):
        return notice_service.audience_label(obj)

    def get_unread(self, obj):
        return obj.pk not in self.context.get("read_ids", set())
