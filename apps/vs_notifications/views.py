# =============================================================================
# vs_notifications / views.py
#
# ViewSets:
#   NotificationViewSet             — user feed (list, retrieve, mark-read, unread-count)
#   NotificationHistoryViewSet      — admin history log (list, retrieve)
#   SchoolNotificationSettingViewSet— school settings (list, bulk partial_update)
#   NotificationTemplateViewSet     — template management (list, create, retrieve, partial_update, preview)
#   NotificationEventTypeViewSet    — event type registry (list, retrieve)
# =============================================================================

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .constants import ChannelChoices, NotificationPermission
from .models import (
    Notification,
    NotificationEventType,
    NotificationTemplate,
    SchoolNotificationSetting,
)
from .permissions import (
    HasAuditPermission,
    HasEnforcePermissionsKey,
    HasTemplateConfigurePermission,
    IsNotificationRecipient,
    IsVisionStaff,
)
from .serializers import (
    MarkReadSerializer,
    NotificationDetailSerializer,
    NotificationEventTypeSerializer,
    NotificationHistoryDetailSerializer,
    NotificationHistorySerializer,
    NotificationListSerializer,
    NotificationTemplatePreviewSerializer,
    NotificationTemplateSerializer,
    SchoolNotificationSettingSerializer,
    SettingsBulkUpdateSerializer,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _school_for_user(user):
    """
    Returns the school FK value for the requesting user, or None for Vision Staff.
    Used to scope querysets to a single school for non-staff users.
    """
    if getattr(user, "is_vision_staff", False):
        return None
    return getattr(user, "school_id", None)


# ---------------------------------------------------------------------------
# 1. NotificationViewSet — user feed
# ---------------------------------------------------------------------------

class NotificationViewSet(viewsets.GenericViewSet,
                          mixins.ListModelMixin,
                          mixins.RetrieveModelMixin):
    """
    Endpoints for the authenticated user's in-app notification feed.

    list          — GET  /notifications/
    retrieve      — GET  /notifications/{id}/
    unread_count  — GET  /notifications/unread-count/
    mark_read     — POST /notifications/mark-read/
    mark_all_read — POST /notifications/mark-all-read/
    """
    permission_classes = [IsAuthenticated]
    serializer_class   = NotificationListSerializer

    def get_queryset(self):
        qs = (
            Notification.objects
            .filter(
                recipient=self.request.user,
                channel=ChannelChoices.IN_APP,
            )
            .select_related("event_type")
            .order_by("-created_at")
        )

        is_read = self.request.query_params.get("is_read")
        if is_read is not None:
            qs = qs.filter(is_read=(is_read.lower() in {"true", "1"}))

        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return NotificationDetailSerializer
        return NotificationListSerializer

    def get_permissions(self):
        if self.action == "retrieve":
            return [IsAuthenticated(), IsNotificationRecipient()]
        return [IsAuthenticated()]

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        count = Notification.objects.filter(
            recipient=request.user,
            channel=ChannelChoices.IN_APP,
            is_read=False,
        ).count()
        return Response({"unread_count": count})

    @action(detail=False, methods=["post"], url_path="mark-read")
    def mark_read(self, request):
        serializer = MarkReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ids = serializer.validated_data["ids"]
        now = timezone.now()

        updated = Notification.objects.filter(
            id__in=ids,
            recipient=request.user,
            channel=ChannelChoices.IN_APP,
            is_read=False,
        ).update(is_read=True, read_at=now)

        return Response({"marked_read": updated})

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        now = timezone.now()
        updated = Notification.objects.filter(
            recipient=request.user,
            channel=ChannelChoices.IN_APP,
            is_read=False,
        ).update(is_read=True, read_at=now)
        return Response({"marked_read": updated})


# ---------------------------------------------------------------------------
# 2. NotificationHistoryViewSet — admin log
# ---------------------------------------------------------------------------

class NotificationHistoryViewSet(viewsets.GenericViewSet,
                                 mixins.ListModelMixin,
                                 mixins.RetrieveModelMixin):
    """
    Full notification dispatch history for admins.

    list    — GET /notifications/history/
    retrieve— GET /notifications/history/{id}/

    Vision Staff see all schools; School Admins see their own school only.
    Requires HasAuditPermission.
    """
    permission_classes = [IsAuthenticated, HasAuditPermission]
    serializer_class   = NotificationHistorySerializer

    def get_serializer_class(self):
        if self.action == "retrieve":
            return NotificationHistoryDetailSerializer
        return NotificationHistorySerializer

    def get_queryset(self):
        qs = Notification.objects.select_related("event_type", "recipient").order_by("-created_at")

        school_id = _school_for_user(self.request.user)
        if school_id is not None:
            qs = qs.filter(school_id=school_id)

        # Optional filters
        qp = self.request.query_params
        if qp.get("channel"):
            qs = qs.filter(channel=qp["channel"])
        if qp.get("status"):
            qs = qs.filter(status=qp["status"])
        if qp.get("event_type_key"):
            qs = qs.filter(event_type__key=qp["event_type_key"])

        return qs


# ---------------------------------------------------------------------------
# 3. SchoolNotificationSettingViewSet — school settings
# ---------------------------------------------------------------------------

class SchoolNotificationSettingViewSet(viewsets.GenericViewSet,
                                       mixins.ListModelMixin):
    """
    Per-school notification settings.

    list          — GET   /notifications/settings/
    partial_update— PATCH /notifications/settings/update/
                   (bulk update — accepts {"updates": [{id, is_enabled}]})

    School Admins see and edit their own school's settings only.
    Requires HasEnforcePermissionsKey.
    """
    permission_classes = [IsAuthenticated, HasEnforcePermissionsKey]
    serializer_class   = SchoolNotificationSettingSerializer

    def get_queryset(self):
        qs = SchoolNotificationSetting.objects.select_related(
            "event_type"
        ).order_by("event_type__source_module", "event_type__key", "channel")

        school_id = _school_for_user(self.request.user)
        if school_id is not None:
            qs = qs.filter(school_id=school_id)

        if self.request.query_params.get("channel"):
            qs = qs.filter(channel=self.request.query_params["channel"])

        return qs

    def partial_update(self, request):
        serializer = SettingsBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updates = serializer.validated_data["updates"]
        school_id = _school_for_user(request.user)

        with transaction.atomic():
            for item in updates:
                qs = SchoolNotificationSetting.objects.filter(id=item["id"])
                if school_id is not None:
                    qs = qs.filter(school_id=school_id)
                qs.update(is_enabled=item["is_enabled"], updated_by=request.user)

        return Response({"updated": len(updates)})


# ---------------------------------------------------------------------------
# 4. NotificationTemplateViewSet — Vision Staff template management
# ---------------------------------------------------------------------------

class NotificationTemplateViewSet(viewsets.GenericViewSet,
                                   mixins.ListModelMixin,
                                   mixins.CreateModelMixin,
                                   mixins.RetrieveModelMixin,
                                   mixins.UpdateModelMixin):
    """
    Notification template CRUD for Vision Staff.

    list          — GET   /notifications/templates/
    create        — POST  /notifications/templates/
    retrieve      — GET   /notifications/templates/{id}/
    partial_update— PATCH /notifications/templates/{id}/
    preview       — POST  /notifications/templates/{id}/preview/
    """
    permission_classes = [IsAuthenticated, IsVisionStaff, HasTemplateConfigurePermission]
    serializer_class   = NotificationTemplateSerializer
    http_method_names  = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = NotificationTemplate.objects.select_related(
            "event_type", "created_by", "updated_by"
        ).order_by("event_type__source_module", "event_type__key", "channel")

        qp = self.request.query_params
        if qp.get("event_type_key"):
            qs = qs.filter(event_type__key=qp["event_type_key"])
        if qp.get("channel"):
            qs = qs.filter(channel=qp["channel"])
        if qp.get("is_active") is not None:
            qs = qs.filter(is_active=qp["is_active"].lower() in {"true", "1"})

        return qs

    @action(detail=True, methods=["post"], url_path="preview")
    def preview(self, request, pk=None):
        template = self.get_object()
        serializer = NotificationTemplatePreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rendered = serializer.render(template)
        return Response(rendered, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# 5. NotificationEventTypeViewSet — read-only registry
# ---------------------------------------------------------------------------

class NotificationEventTypeViewSet(viewsets.GenericViewSet,
                                    mixins.ListModelMixin,
                                    mixins.RetrieveModelMixin):
    """
    Read-only view of the platform event type registry.

    list    — GET /notifications/event-types/
    retrieve— GET /notifications/event-types/{id}/

    Available to all authenticated users (used by settings UI and template editor).
    """
    permission_classes = [IsAuthenticated]
    serializer_class   = NotificationEventTypeSerializer

    def get_queryset(self):
        qs = NotificationEventType.objects.order_by("source_module", "key")

        qp = self.request.query_params
        if qp.get("source_module"):
            qs = qs.filter(source_module=qp["source_module"])
        if qp.get("is_active") is not None:
            qs = qs.filter(is_active=qp["is_active"].lower() in {"true", "1"})

        return qs
