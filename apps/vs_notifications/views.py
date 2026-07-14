# =============================================================================
# vs_notifications / views.py
#
# ViewSets for vs_notifications.
#
# Endpoint groups:
#   NotificationViewSet          — user feed, mark-read, unread count
#   NotificationHistoryViewSet   — admin history log
#   NotificationSettingViewSet   — effective settings matrix (read) + upsert
#   NotificationTemplateViewSet  — Vision Staff template management
#   NotificationEventTypeViewSet — event type catalogue (read-only, all users)
#
# Scoping model (the platform is global; school is optional):
#   * School-scoped users act on their own school. Supplying ?school= for a
#     DIFFERENT school returns 404 (never leak another school's existence);
#     their own id is allowed.
#   * CX staff (platform tenant) default to the PLATFORM scope, or pass
#     ?school=<id> to view/write a specific school's rows.
#
# NOTE on managers: view scoping is done EXPLICITLY (recipient=… / school=… /
# all_objects) rather than relying on the ambient TenantAwareManager — the
# tenant thread-local is not reliably set for DRF-authenticated requests, and
# explicit scoping is the security-critical contract here.
# =============================================================================

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from core.pagination import XVSPagination
from core.response import error_response, success_response

from .constants import (
    ChannelChoices,
    NotificationErrorCode,
    NotificationPermission,
)
from .exceptions import FilterRequiredError
from .models import (
    Notification,
    NotificationEventType,
    NotificationSetting,
    NotificationTemplate,
)
from vs_rbac.permissions import HasRBACPermission

from .serializers import (
    MarkReadSerializer,
    NotificationDetailSerializer,
    NotificationEventTypeSerializer,
    NotificationHistoryDetailSerializer,
    NotificationHistorySerializer,
    NotificationListSerializer,
    NotificationTemplatePreviewSerializer,
    NotificationTemplateSerializer,
    SettingsBulkUpdateSerializer,
)
from .services.settings import resolve_channels_bulk


logger = logging.getLogger("vs_notifications.views")

# Sentinel used by history + settings to mean "the platform (school IS NULL) rows".
_PLATFORM_SCOPE = "platform"


# ---------------------------------------------------------------------------
# 1.  Notification feed (user-facing)
# ---------------------------------------------------------------------------

class NotificationViewSet(viewsets.GenericViewSet):
    """
    Handles the authenticated user's in-app notification feed.

    Every queryset is scoped to the requesting user — no cross-user access.

    Routes:
        GET  /notifications/              — paginated feed (in-app only)
        GET  /notifications/unread-count/ — bell badge count
        POST /notifications/mark-read/    — mark list of IDs as read
        POST /notifications/mark-all-read/— mark all unread as read
        GET  /notifications/{id}/         — single record detail

    docstring-name: My notifications
    """
    permission_classes = [IsAuthenticated]
    pagination_class   = XVSPagination

    def get_queryset(self):
        """
        Scope to the requesting user's in-app notifications only.
        Prefetch event_type to avoid N+1 on serialization.
        """
        # Feed access is recipient-owned; email/history rows are excluded here.
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
        return success_response("Notifications retrieved.", data=serializer.data)

    def retrieve(self, request, pk=None):
        """
        GET /notifications/{id}/ — single in-app notification detail.

        Strictly scoped to the requesting user's IN_APP notifications. Anything
        else (another user's record, an email record, a bad id) returns 404 —
        we never leak existence with a 403, and staff use the history endpoint.
        """
        try:
            notif = (
                self.get_queryset().get(pk=pk)
            )
        except Notification.DoesNotExist:
            return error_response(
                "Notification not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = NotificationDetailSerializer(notif)
        return success_response("Notification retrieved.", data=serializer.data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        """
        GET /notifications/unread-count/
        Count of unread in-app notifications. Drives the bell badge.
        """
        count = Notification.objects.filter(
            recipient=request.user,
            channel=ChannelChoices.IN_APP,
            is_read=False,
        ).count()
        return success_response("Unread count retrieved.", data={"unread_count": count})

    @action(detail=False, methods=["post"], url_path="mark-read")
    def mark_read(self, request):
        """
        POST /notifications/mark-read/
        Mark a list of notification IDs as read. Only the requesting user's
        IN_APP notifications are updated — foreign or EMAIL ids are skipped.
        """
        serializer = MarkReadSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid request.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        ids = serializer.validated_data["ids"]
        now = timezone.now()

        with transaction.atomic():
            # Foreign IDs and EMAIL rows are ignored instead of leaking why they failed.
            updated = Notification.objects.filter(
                id__in=ids,
                recipient=request.user,
                channel=ChannelChoices.IN_APP,
                is_read=False,
            ).update(is_read=True, read_at=now)

        return success_response(
            f"{updated} notification(s) marked as read.",
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
            # Bulk read state only applies to in-app feed rows for this user.
            updated = Notification.objects.filter(
                recipient=request.user,
                channel=ChannelChoices.IN_APP,
                is_read=False,
            ).update(is_read=True, read_at=now)

        return success_response(
            f"All {updated} unread notification(s) marked as read.",
            data={"updated_count": updated},
        )


# ---------------------------------------------------------------------------
# 2.  Notification history (admin)
# ---------------------------------------------------------------------------

class NotificationHistoryViewSet(viewsets.GenericViewSet):
    """
    Admin notification history log.

    School Admins: see EXACTLY their own school's rows (never platform rows).
    CX staff:      see everything; must supply ≥1 filter. Pass
                   ``scope=platform`` to filter to platform rows (school IS NULL).

    Routes:
        GET /notifications/history/      — paginated log
        GET /notifications/history/{id}/ — full detail record

    docstring-name: Notification history
    """
    permission_classes = [IsAuthenticated, HasRBACPermission]
    rbac_permission = NotificationPermission.AUDIT_ACTIVITY
    pagination_class   = XVSPagination

    def get_queryset(self):
        """
        Base queryset, scoped by the caller's tenancy.

        School admins are hard-scoped to their own school (all_objects so the
        include_global manager change can't leak platform rows into their view).
        CX staff (no school) see everything.
        """
        user = self.request.user
        # History uses the unscoped manager so platform rows are visible only when allowed.
        qs = (
            Notification.all_objects
            .select_related("recipient", "event_type")
            .order_by("-created_at")
        )
        return qs.filter(tenant=self.request.tenant)

    def _apply_filters(self, qs, params, is_vision_staff: bool):
        """Apply query param filters to the history queryset."""
        scope           = params.get("scope")
        recipient_email = params.get("recipient_email")
        event_type_key  = params.get("event_type_key")
        channel         = params.get("channel")
        status_param    = params.get("status")
        created_after   = params.get("created_after")
        created_before  = params.get("created_before")

        # Everyone (school admins included, who have an implicit school filter)
        # must narrow the log with at least one explicit filter, so a school-less
        # CX user cannot dump the entire table unfiltered.
        if not any([
            scope, recipient_email, event_type_key,
            channel, status_param, created_after, created_before,
        ]):
            raise FilterRequiredError()

        if scope == _PLATFORM_SCOPE:
            qs = qs.filter(tenant__kind="PLATFORM")
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
                exc.message,
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code=NotificationErrorCode.FILTER_REQUIRED,
            )

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = NotificationHistorySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = NotificationHistorySerializer(qs, many=True)
        return success_response("History retrieved.", data=serializer.data)

    def retrieve(self, request, pk=None):
        """GET /notifications/history/{id}/"""
        qs = self.get_queryset()
        try:
            notif = qs.get(pk=pk)
        except Notification.DoesNotExist:
            return error_response(
                "Notification not found.",
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = NotificationHistoryDetailSerializer(notif)
        return success_response("Notification retrieved.", data=serializer.data)


# ---------------------------------------------------------------------------
# 3.  Notification settings (effective matrix + overrides)
# ---------------------------------------------------------------------------

class NotificationSettingViewSet(viewsets.GenericViewSet):
    """
    Notification settings — the EFFECTIVE matrix and per-scope overrides.

    GET   /notifications/settings/        — full effective matrix for the scope.
    PATCH /notifications/settings/update/ — upsert override rows by
                                            (event_type_key, channel).

    Scope resolution (same for GET and PATCH):
      * School-scoped caller → their own school, overlaid on platform defaults.
        ?school=<own id> is allowed; ?school=<other id> → 404 (no leak).
      * CX staff (platform tenant) → platform scope by default;
        ?school=<id> targets that school's effective matrix / rows.

    Transactional event types appear in the matrix flagged
    ``is_transactional: true`` and are read-only (they bypass settings) — a
    PATCH touching one is rejected.

    Permission: communication.communication_permissions.enforce (RBAC).

    docstring-name: Notification settings
    """
    permission_classes = [IsAuthenticated, HasRBACPermission]
    rbac_permission    = NotificationPermission.ENFORCE_PERMISSIONS

    # ── Scope helpers ──────────────────────────────────────────────────────

    def _resolve_scope(self, request):
        """
        Resolve the settings scope from the asserted tenant.

        A business tenant manages its own override rows. A PLATFORM-kind tenant
        (Codex staff) manages the platform DEFAULT layer — the tenant-NULL rows
        every school inherits. Writing codex-tenant rows here would be inert for
        schools: dispatch resolution only reads (tenant IS NULL | own tenant).
        Returns (tenant_or_none, None); None means the platform layer.
        """
        tenant = request.tenant
        if getattr(tenant, "kind", None) == "PLATFORM":
            return None, None
        return tenant, None

    def _build_matrix(self, tenant):
        """
        Build the effective settings matrix for a scope.

        For each active event type × supported channel, resolve the effective
        value and record which layer produced it. resolve_channels_bulk owns the
        is_active / transactional / layering logic; the same fetched rows also
        feed the `source` label ("school" / "platform" / "default") so the UI
        can render provenance. Total cost: 1 event-type query + 1 settings query.
        """
        event_types = list(
            NotificationEventType.objects.filter(is_active=True)
            .order_by("source_module", "key")
        )

        # One settings query for the whole matrix. Materialised to a list so it
        # feeds both the provenance sets below and the bulk resolver without
        # re-querying.
        from django.db.models import Q
        scope_q = Q(tenant__isnull=True) | Q(tenant=tenant)
        rows = list(
            NotificationSetting.all_objects.filter(
                scope_q, event_type__in=event_types,
            ).values("event_type_id", "channel", "is_enabled", "tenant_id")
        )

        # Which (event_type_id, channel) have a school row / platform row?
        school_rows = set()
        platform_rows = set()
        for r in rows:
            key = (r["event_type_id"], r["channel"])
            if r["tenant_id"] is None:
                platform_rows.add(key)
            else:
                school_rows.add(key)

        # Layering rules live in the service — pass the pre-fetched rows through.
        resolved_by_et = resolve_channels_bulk(event_types, tenant=tenant, rows=rows)

        matrix = []
        for et in event_types:
            resolved = resolved_by_et[et.id]
            for channel in et.supported_channels:
                key = (et.id, channel)
                if et.is_transactional or not et.is_active:
                    source = "default"
                elif tenant is not None and key in school_rows:
                    source = "tenant"
                elif key in platform_rows:
                    source = "platform"
                else:
                    source = "default"
                matrix.append({
                    "event_type_key":   et.key,
                    "event_type_label": et.label,
                    "source_module":    et.source_module,
                    "channel":          channel,
                    "is_enabled":       resolved.get(channel, False),
                    "is_transactional": et.is_transactional,
                    "source":           source,
                })
        return matrix

    # ── Read ───────────────────────────────────────────────────────────────

    def list(self, request):
        """GET /notifications/settings/ — the effective matrix for the scope."""
        tenant, denied = self._resolve_scope(request)
        if denied is not None:
            return denied

        matrix = self._build_matrix(tenant)
        return success_response("Settings retrieved.", data=matrix)

    # ── Write ──────────────────────────────────────────────────────────────

    def partial_update(self, request):
        """
        PATCH /notifications/settings/update/
        Upsert override rows by (event_type_key, channel). Atomic.

        Rejections (400 with field errors):
          * unknown event key / unknown or unsupported channel
          * disabling IN_APP (IN_APP_ALWAYS_ENABLED)
          * toggling a transactional event type (TRANSACTIONAL_NOT_CONFIGURABLE)
        """
        tenant, denied = self._resolve_scope(request)
        if denied is not None:
            return denied

        serializer = SettingsBulkUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid request.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        updates = serializer.validated_data["updates"]

        # Resolve all referenced event types up front (one query).
        keys = {u["event_type_key"] for u in updates}
        event_types = {
            et.key: et
            for et in NotificationEventType.objects.filter(key__in=keys, is_active=True)
        }

        errors = []
        for idx, item in enumerate(updates):
            key = item["event_type_key"]
            channel = item["channel"]
            is_enabled = item["is_enabled"]

            et = event_types.get(key)
            if et is None:
                errors.append({
                    "index": idx,
                    "error_code": NotificationErrorCode.UNKNOWN_EVENT_TYPE,
                    "message": f"Unknown or inactive event type: '{key}'.",
                })
                continue
            if channel not in ChannelChoices.ALL:
                # Reject unknown channel strings before checking event-specific support.
                errors.append({
                    "index": idx,
                    "error_code": NotificationErrorCode.UNKNOWN_CHANNEL,
                    "message": f"Unknown channel: '{channel}'.",
                })
                continue
            if channel not in et.supported_channels:
                # A known channel can still be invalid for this event type.
                errors.append({
                    "index": idx,
                    "error_code": NotificationErrorCode.UNSUPPORTED_CHANNEL,
                    "message": f"Channel '{channel}' is not supported by '{key}'.",
                })
                continue
            if et.is_transactional:
                # Must-send events ignore settings rows, so overrides would be misleading.
                errors.append({
                    "index": idx,
                    "error_code": NotificationErrorCode.TRANSACTIONAL_NOT_CONFIGURABLE,
                    "message": (
                        f"'{key}' is a transactional event and cannot be "
                        "configured — it always dispatches."
                    ),
                })
                continue
            if channel == ChannelChoices.IN_APP and is_enabled is False:
                # Product policy keeps the in-app audit/feed trail always enabled.
                errors.append({
                    "index": idx,
                    "error_code": NotificationErrorCode.IN_APP_ALWAYS_ENABLED,
                    "message": "The in-app channel cannot be disabled.",
                })
                continue

        if errors:
            return error_response(
                "One or more updates were rejected.",
                error={"updates": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # All valid — upsert override rows by (school, event_type, channel).
        with transaction.atomic():
            # The full PATCH is all-or-nothing so settings cannot partially apply.
            for item in updates:
                et = event_types[item["event_type_key"]]
                NotificationSetting.all_objects.update_or_create(
                    tenant=tenant,
                    event_type=et,
                    channel=item["channel"],
                    defaults={
                        "is_enabled": item["is_enabled"],
                        "updated_by": request.user,
                    },
                )

        # Return the updated effective entries (fresh resolve).
        matrix = self._build_matrix(tenant)
        touched = {(u["event_type_key"], u["channel"]) for u in updates}
        updated_entries = [
            row for row in matrix
            if (row["event_type_key"], row["channel"]) in touched
        ]
        return success_response(
            f"{len(updates)} setting(s) updated.",
            data=updated_entries,
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
    POST  /notifications/templates/{id}/preview/— render preview (incl. html_body)

    Permission: communication.notification_templates.configure (RBAC).

    docstring-name: Notification templates
    """
    permission_classes = [IsAuthenticated, HasRBACPermission]
    rbac_permission    = NotificationPermission.TEMPLATE_CONFIGURE

    def get_queryset(self):
        # Templates are global catalogue records, not school-scoped rows.
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
        return success_response("Templates retrieved.", data=serializer.data)

    def create(self, request):
        """POST /notifications/templates/"""
        serializer = NotificationTemplateSerializer(
            data=request.data,
            context={"request": request},
        )
        if not serializer.is_valid():
            return error_response(
                "Invalid template data.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            template = serializer.save()
        except Exception as exc:
            # Catch unique_together violation on (event_type, channel)
            if "unique" in str(exc).lower():
                return error_response(
                    "A template for this event type and channel already exists. "
                    "Update the existing template instead.",
                    status=status.HTTP_409_CONFLICT,
                    code=NotificationErrorCode.DUPLICATE_TEMPLATE,
                )
            raise

        return success_response(
            "Template created.",
            data=NotificationTemplateSerializer(template).data,
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None):
        """GET /notifications/templates/{id}/"""
        try:
            template = self.get_queryset().get(pk=pk)
        except NotificationTemplate.DoesNotExist:
            return error_response(
                "Template not found.",
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = NotificationTemplateSerializer(template)
        return success_response("Template retrieved.", data=serializer.data)

    def partial_update(self, request, pk=None):
        """PATCH /notifications/templates/{id}/"""
        try:
            template = self.get_queryset().get(pk=pk)
        except NotificationTemplate.DoesNotExist:
            return error_response(
                "Template not found.",
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
                "Invalid template data.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = serializer.save()
        return success_response(
            "Template updated.",
            data=NotificationTemplateSerializer(updated).data,
        )

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
                "Template not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = NotificationTemplatePreviewSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Invalid preview request.",
                error=serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        rendered = serializer.render(template)
        # Preview renders content only; it never creates Notification records or sends mail.
        return success_response("Preview rendered.", data=rendered)


# ---------------------------------------------------------------------------
# 5.  Notification event type catalogue (read-only, all authenticated users)
# ---------------------------------------------------------------------------

class NotificationEventTypeViewSet(viewsets.GenericViewSet):
    """
    Read-only event type catalogue.
    Accessible to all authenticated users.

    GET /notifications/event-types/      — list all active event types
    GET /notifications/event-types/{id}/ — retrieve single event type

    docstring-name: Notification event types
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
        return success_response("Event types retrieved.", data=serializer.data)

    def retrieve(self, request, pk=None):
        """GET /notifications/event-types/{id}/"""
        try:
            event_type = self.get_queryset().get(pk=pk)
        except NotificationEventType.DoesNotExist:
            return error_response(
                "Event type not found.",
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = NotificationEventTypeSerializer(event_type)
        return success_response("Event type retrieved.", data=serializer.data)
