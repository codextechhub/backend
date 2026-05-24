# =============================================================================
# vs_notifications / views.py
#
# ViewSets and views for vs_notifications.
#
# Endpoint groups:
#   NotificationViewSet          — user feed, mark-read, unread count
#   NotificationHistoryViewSet   — admin history log
#   SchoolNotificationSettingViewSet — school-level settings
#   NotificationTemplateViewSet  — Vision Staff template management
#   NotificationEventTypeViewSet — event type catalogue (read-only, all users)
# =============================================================================

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.pagination import XVSPagination
from core.response import error_response, success_response

from .constants import ChannelChoices, NotificationErrorCode, NotificationStatus
from .exceptions import FilterRequiredError
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

logger = logging.getLogger("vs_notifications.views")


# ---------------------------------------------------------------------------
# 1.  Notification feed (user-facing)
# ---------------------------------------------------------------------------

class NotificationViewSet(viewsets.GenericViewSet):
    """
    Handles the authenticated user's in-app notification feed.

    All querysets are scoped to the requesting user — no cross-user access.

    Routes:
        GET  /notifications/              — paginated feed (in-app only)
        GET  /notifications/unread-count/ — bell badge count
        POST /notifications/mark-read/    — mark list of IDs as read
        POST /notifications/mark-all-read/— mark all unread as read
        GET  /notifications/{id}/         — single record detail
    """
    permission_classes = [IsAuthenticated]
    pagination_class   = XVSPagination

    def get_queryset(self):
        """
        Scope to the requesting user's in-app notifications only.
        Prefetch event_type to avoid N+1 on serialization.
        """
        return (
            Notification.objects
            .filter(
                recipient=self.request.user,
                channel=ChannelChoices.IN_APP,
            )
            .select_related("event_type")
            .order_by("-created_at")
        )

    def list(self, request):
        """GET /notifications/ — paginated in-app feed with optional filters."""
        qs = self.get_queryset()

        # Optional filters
        is_read = request.query_params.get("is_read")
        if is_read is not None:
            qs = qs.filter(is_read=is_read.lower() == "true")

        event_type_key = request.query_params.get("event_type_key")
        if event_type_key:
            qs = qs.filter(event_type__key=event_type_key)

        created_after = request.query_params.get("created_after")
        if created_after:
            qs = qs.filter(created_at__gte=created_after)

        created_before = request.query_params.get("created_before")
        if created_before:
            qs = qs.filter(created_at__lte=created_before)

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = NotificationListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = NotificationListSerializer(qs, many=True)
        return success_response(data=serializer.data)

    def retrieve(self, request, pk=None):
        """GET /notifications/{id}/ — single notification detail."""
        try:
            notif = Notification.objects.select_related("event_type").get(pk=pk)
        except Notification.DoesNotExist:
            return error_response(
                message="Notification not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        # Object-level permission check
        perm = IsNotificationRecipient()
        if not perm.has_object_permission(request, self, notif):
            return error_response(
                code=NotificationErrorCode.ACCESS_DENIED,
                message="You do not have permission to access this notification.",
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = NotificationDetailSerializer(notif)
        return success_response(data=serializer.data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        """
        GET /notifications/unread-count/
        Returns the count of unread in-app notifications.
        Lightweight — used to drive the bell badge.
        """
        count = Notification.objects.filter(
            recipient=request.user,
            channel=ChannelChoices.IN_APP,
            is_read=False,
        ).count()
        return success_response(data={"unread_count": count})

    @action(detail=False, methods=["post"], url_path="mark-read")
    def mark_read(self, request):
        """
        POST /notifications/mark-read/
        Mark a list of notification IDs as read.
        Only the requesting user's IN_APP notifications are updated —
        IDs belonging to other users or EMAIL notifications are silently skipped.
        """
        serializer = MarkReadSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                message="Invalid request.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        ids = serializer.validated_data["ids"]
        now = timezone.now()

        with transaction.atomic():
            updated = Notification.objects.filter(
                id__in=ids,
                recipient=request.user,
                channel=ChannelChoices.IN_APP,
                is_read=False,
            ).update(is_read=True, read_at=now)

        return success_response(
            message=f"{updated} notification(s) marked as read.",
            data={"updated_count": updated},
        )

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        """
        POST /notifications/mark-all-read/
        Mark all unread in-app notifications for the requesting user as read.
        """
        now = timezone.now()

        with transaction.atomic():
            updated = Notification.objects.filter(
                recipient=request.user,
                channel=ChannelChoices.IN_APP,
                is_read=False,
            ).update(is_read=True, read_at=now)

        return success_response(
            message=f"All {updated} unread notification(s) marked as read.",
            data={"updated_count": updated},
        )


# ---------------------------------------------------------------------------
# 2.  Notification history (admin)
# ---------------------------------------------------------------------------

class NotificationHistoryViewSet(viewsets.GenericViewSet):
    """
    Admin notification history log.

    School Admins: see their school's notifications only.
    Vision Staff:  see all schools (requires at least one filter).

    Routes:
        GET /notifications/history/      — paginated log
        GET /notifications/history/{id}/ — full detail record
    """
    permission_classes = [IsAuthenticated, HasAuditPermission]
    pagination_class   = XVSPagination

    def get_queryset(self):
        user = self.request.user
        qs = (
            Notification.objects
            .select_related("recipient", "event_type")
            .order_by("-created_at")
        )
        if not getattr(user, "is_vision_staff", False):
            qs = qs.filter(school=user.school)
        return qs

    def _apply_filters(self, qs, params, is_vision_staff: bool):
        """Apply query param filters to the history queryset."""
        school_id       = params.get("school_id")
        recipient_email = params.get("recipient_email")
        event_type_key  = params.get("event_type_key")
        channel         = params.get("channel")
        status_param    = params.get("status")
        created_after   = params.get("created_after")
        created_before  = params.get("created_before")

        # Vision Staff must supply at least one filter
        if is_vision_staff and not any([
            school_id, recipient_email, event_type_key,
            channel, status_param, created_after, created_before,
        ]):
            raise FilterRequiredError()

        if school_id:
            qs = qs.filter(school_id=school_id)
        if recipient_email:
            qs = qs.filter(recipient__email__icontains=recipient_email)
        if event_type_key:
            qs = qs.filter(event_type__key=event_type_key)
        if channel:
            qs = qs.filter(channel=channel)
        if status_param:
            qs = qs.filter(status=status_param)
        if created_after:
            qs = qs.filter(created_at__gte=created_after)
        if created_before:
            qs = qs.filter(created_at__lte=created_before)

        return qs

    def list(self, request):
        """GET /notifications/history/"""
        is_vision_staff = getattr(request.user, "is_vision_staff", False)
        qs = self.get_queryset()

        try:
            qs = self._apply_filters(qs, request.query_params, is_vision_staff)
        except FilterRequiredError as exc:
            return error_response(
                code=NotificationErrorCode.FILTER_REQUIRED,
                message=exc.message,
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = NotificationHistorySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = NotificationHistorySerializer(qs, many=True)
        return success_response(data=serializer.data)

    def retrieve(self, request, pk=None):
        """GET /notifications/history/{id}/"""
        qs = self.get_queryset()
        try:
            notif = qs.get(pk=pk)
        except Notification.DoesNotExist:
            return error_response(
                message="Notification not found.",
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = NotificationHistoryDetailSerializer(notif)
        return success_response(data=serializer.data)


# ---------------------------------------------------------------------------
# 3.  School notification settings
# ---------------------------------------------------------------------------

class SchoolNotificationSettingViewSet(viewsets.GenericViewSet):
    """
    School-level notification settings.

    GET  /notifications/settings/  — list all settings for the school,
                                     grouped by source module.
    PATCH /notifications/settings/ — bulk update is_enabled values.

    Permission: HasEnforcePermissionsKey (School Admin role).
    Queryset: strictly scoped to the requesting user's school.
    """
    permission_classes = [IsAuthenticated, HasEnforcePermissionsKey]

    def get_queryset(self):
        return (
            SchoolNotificationSetting.objects
            .filter(school=self.request.user.school)
            .select_related("event_type")
            .order_by("event_type__source_module", "event_type__key", "channel")
        )

    def list(self, request):
        """GET /notifications/settings/"""
        qs = self.get_queryset()
        serializer = SchoolNotificationSettingSerializer(qs, many=True)
        return success_response(data=serializer.data)

    def partial_update(self, request):
        """
        PATCH /notifications/settings/
        Bulk update is_enabled values. All changes commit atomically.
        """
        serializer = SettingsBulkUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                message="Invalid request.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        updates = serializer.validated_data["updates"]
        setting_ids = [item["id"] for item in updates]

        # Build map of existing settings (scoped to this school for safety)
        settings_map = {
            s.id: s
            for s in SchoolNotificationSetting.objects.filter(
                id__in=setting_ids,
                school=request.user.school,
            )
        }

        updated_settings = []
        with transaction.atomic():
            for item in updates:
                setting = settings_map.get(item["id"])
                if setting is None:
                    # ID not found or belongs to another school — skip silently
                    continue
                setting.is_enabled = item["is_enabled"]
                setting.updated_by = request.user
                updated_settings.append(setting)

            SchoolNotificationSetting.objects.bulk_update(
                updated_settings, ["is_enabled", "updated_by", "updated_at"]
            )

        result_serializer = SchoolNotificationSettingSerializer(
            updated_settings, many=True
        )
        return success_response(
            message=f"{len(updated_settings)} setting(s) updated.",
            data=result_serializer.data,
        )


# ---------------------------------------------------------------------------
# 4.  Notification template management (Vision Staff only)
# ---------------------------------------------------------------------------

class NotificationTemplateViewSet(viewsets.GenericViewSet):
    """
    Notification template management.

    GET   /notifications/templates/             — list all templates
    POST  /notifications/templates/             — create template
    GET   /notifications/templates/{id}/        — retrieve single
    PATCH /notifications/templates/{id}/        — update
    POST  /notifications/templates/{id}/preview/— render preview

    Permission: HasTemplateConfigurePermission (Vision Staff only).
    """
    permission_classes = [IsAuthenticated, HasTemplateConfigurePermission]

    def get_queryset(self):
        return (
            NotificationTemplate.objects
            .select_related("event_type", "created_by", "updated_by")
            .order_by("event_type__source_module", "event_type__key", "channel")
        )

    def list(self, request):
        """GET /notifications/templates/"""
        qs = self.get_queryset()

        event_type_key = request.query_params.get("event_type_key")
        if event_type_key:
            qs = qs.filter(event_type__key=event_type_key)

        channel = request.query_params.get("channel")
        if channel:
            qs = qs.filter(channel=channel)

        serializer = NotificationTemplateSerializer(qs, many=True)
        return success_response(data=serializer.data)

    def create(self, request):
        """POST /notifications/templates/"""
        serializer = NotificationTemplateSerializer(
            data=request.data,
            context={"request": request},
        )
        if not serializer.is_valid():
            return error_response(
                message="Invalid template data.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            template = serializer.save()
        except Exception as exc:
            # Catch unique_together violation on (event_type, channel)
            if "unique" in str(exc).lower():
                return error_response(
                    code=NotificationErrorCode.DUPLICATE_TEMPLATE,
                    message=(
                        "A template for this event type and channel already exists. "
                        "Update the existing template instead."
                    ),
                    status=status.HTTP_409_CONFLICT,
                )
            raise

        return success_response(
            data=NotificationTemplateSerializer(template).data,
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None):
        """GET /notifications/templates/{id}/"""
        try:
            template = self.get_queryset().get(pk=pk)
        except NotificationTemplate.DoesNotExist:
            return error_response(
                message="Template not found.",
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = NotificationTemplateSerializer(template)
        return success_response(data=serializer.data)

    def partial_update(self, request, pk=None):
        """PATCH /notifications/templates/{id}/"""
        try:
            template = self.get_queryset().get(pk=pk)
        except NotificationTemplate.DoesNotExist:
            return error_response(
                message="Template not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = NotificationTemplateSerializer(
            template,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        if not serializer.is_valid():
            return error_response(
                message="Invalid template data.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = serializer.save()
        return success_response(data=NotificationTemplateSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="preview")
    def preview(self, request, pk=None):
        """
        POST /notifications/templates/{id}/preview/
        Render the template with sample context. Does not send anything.
        """
        try:
            template = self.get_queryset().get(pk=pk)
        except NotificationTemplate.DoesNotExist:
            return error_response(
                message="Template not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = NotificationTemplatePreviewSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                message="Invalid preview request.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        rendered = serializer.render(template)
        return success_response(data=rendered)


# ---------------------------------------------------------------------------
# 5.  Notification event type catalogue (read-only, all authenticated users)
# ---------------------------------------------------------------------------

class NotificationEventTypeViewSet(viewsets.GenericViewSet):
    """
    Read-only event type catalogue.
    Accessible to all authenticated users.
    Used by the School Admin settings UI and the Vision Staff template editor.

    GET /notifications/event-types/      — list all active event types
    GET /notifications/event-types/{id}/ — retrieve single event type
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return NotificationEventType.objects.filter(is_active=True).order_by(
            "source_module", "key"
        )

    def list(self, request):
        """GET /notifications/event-types/"""
        qs = self.get_queryset()
        serializer = NotificationEventTypeSerializer(qs, many=True)
        return success_response(data=serializer.data)

    def retrieve(self, request, pk=None):
        """GET /notifications/event-types/{id}/"""
        try:
            event_type = self.get_queryset().get(pk=pk)
        except NotificationEventType.DoesNotExist:
            return error_response(
                message="Event type not found.",
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = NotificationEventTypeSerializer(event_type)
        return success_response(data=serializer.data)
