"""ViewSets for vs_notifications.

Endpoint groups:
  NotificationViewSet          - user feed, mark-read, unread count
  NotificationHistoryViewSet   - admin history log
  NotificationSettingViewSet   - effective settings matrix (read) + upsert
  NotificationTemplateViewSet  - Vision Staff template management
  NotificationEventTypeViewSet - event type catalogue (read-only, all users)

Scoping model (scope comes from the ASSERTED TENANT, not a query parameter):
  * There is no ?school= parameter. Callers assert a tenant with ?tenant=<slug>
    and get that tenant's rows. Asserting a slug that is not the caller's own
    tenant is refused with 404 in vs_rbac.authentication (never leak another
    tenant's existence); none of these views opt in via
    platform_cross_tenant_param, so not even CX staff can cross over here.
  * A PLATFORM-kind tenant (CX staff) is Codex's own tenant. In the settings
    endpoints it resolves to the PLATFORM scope - the tenant-NULL default rows
    every tenant inherits, NOT codex's own rows.
  * The settings endpoints alone also take ``?branch=<id>``, which narrows a
    business tenant's scope to one of its branches; see
    NotificationSettingViewSet for who may name which branch.

NOTE on managers: view scoping is done EXPLICITLY (recipient=… / tenant=… /
all_objects) rather than relying on the ambient TenantAwareManager - the
tenant thread-local is not reliably set for DRF-authenticated requests, and
explicit scoping is the security-critical contract here.
"""
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated

from core.pagination import XVSPagination
from core.response import error_response, success_response

from .constants import (
    ChannelChoices,
    NotificationErrorCode,
    NotificationPermission,
)
from .exceptions import FilterRequiredError
from . import labels as notification_labels
from .models import (
    Notification,
    NotificationEventType,
    NotificationSetting,
    NotificationTemplate,
)
from vs_rbac.permissions import HasRBACPermission, TenantSurfaceAllowed

from .serializers import (
    AcknowledgeRouteSerializer,
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
from .services.audit import (
    record_setting_change,
    record_template_change,
    template_snapshot,
)
from .services.settings import SOURCE_DEFAULT, resolve_settings_bulk, settings_rows
from .services.routing import notification_route_q


logger = logging.getLogger("vs_notifications.views")

# Sentinel for the history ``?scope=`` filter, meaning "rows owned by the
# PLATFORM-kind tenant". Settings no longer uses it: its scope is the asserted
# tenant.
_PLATFORM_SCOPE = "platform"

# Longer terms are noise, not search: a 100k-character icontains scan is a free
# way to make the database work for nothing.
SEARCH_MAX_LENGTH = 120


# Match a free-text term against what the reader can actually see on a row.
def _feed_search_q(term: str) -> Q:
    """Search the rendered message and the event's label, nothing internal."""
    return (
        Q(subject__icontains=term)
        | Q(body__icontains=term)
        | Q(event_type__label__icontains=term)
    )


def _unread_in_app_count(user) -> int:
    """The bell badge number for *user*.

    Shared by the count endpoint and by every response that reports the badge,
    so the two can never describe the same inbox differently.
    """
    return Notification.objects.filter(
        recipient=user,
        channel=ChannelChoices.IN_APP,
        is_read=False,
    ).count()


# ---------------------------------------------------------------------------
# 1.  Notification feed (user-facing)
# ---------------------------------------------------------------------------

class NotificationViewSet(viewsets.GenericViewSet):
    """
    Handles the authenticated user's in-app notification feed.

    Every queryset is scoped to the requesting user - no cross-user access.

    Routes:
        GET  /notifications/              - paginated feed (in-app only)
        GET  /notifications/unread-count/ - bell badge count
        POST /notifications/mark-read/    - mark list of IDs as read
        POST /notifications/mark-all-read/- mark all unread as read
        POST /notifications/acknowledge-route/ - mark viewed destination events
        GET  /notifications/{id}/         - single record detail

    Feed order: UNREAD FIRST, newest first within each group. Opening the inbox
    should show what still needs attention, not what happens to be newest.
    ``?is_read=`` narrows to one group (the Unread / Read tabs); the default,
    unfiltered call is the All tab and keeps unread on top.

    docstring-name: My notifications
    """
    # A bare IsAuthenticated replaces DEFAULT_PERMISSION_CLASSES, which would
    # drop the pending-tenant surface gate (FR-012), so it is named here.
    permission_classes = [IsAuthenticated, TenantSurfaceAllowed]

    # Open to a tenant that has not gone live yet (FR-012). Onboarding writes
    # in-app notifications to a school WHILE it is still PENDING
    # (onboarding.step_completed, onboarding.go_live_ready), so if this stayed
    # closed the school would be sent messages it could not read until go-live.
    # Do not remove this as an oversight: the inbox has to be readable before
    # activation for those messages to mean anything.
    #
    # The actions are listed rather than declared as ``True`` so that the
    # opening covers today's personal-inbox surface only. Every one of these is
    # recipient-owned (see get_queryset: recipient=request.user, IN_APP only);
    # anything added to this ViewSet later stays closed until someone decides
    # it belongs here too. The rest of the module - settings, the event-type
    # catalogue, history, templates - remains shut to a pending tenant.
    pending_tenant_surface = (
        "list",
        "retrieve",
        "unread_count",
        "mark_read",
        "mark_all_read",
        "acknowledge_route",
    )
    pagination_class   = XVSPagination

    def get_queryset(self):
        """
        Scope to the requesting user's in-app notifications only.

        Ordering is (unread first, newest first, id) and the trailing id is not
        decoration: dispatch bulk-creates a batch of records with identical
        created_at values, and without a unique tiebreaker the database is free
        to order those ties differently per query - which makes rows repeat or
        vanish between page 1 and page 2. The
        (recipient, channel, is_read, -created_at) index covers this order.
        """
        # Feed access is recipient-owned; email/history rows are excluded here.
        return (
            Notification.objects
            .filter(
                recipient=self.request.user,
                channel=ChannelChoices.IN_APP,
            )
            .select_related("event_type")
            .order_by("is_read", "-created_at", "id")
        )

    def list(self, request):
        """GET /notifications/ - paginated in-app feed with optional filters."""
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

        # Search is a QUERYSET filter, deliberately: filtering the page in the
        # browser instead leaves the page count, the totals and every page after
        # the first describing the unsearched list.
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(_feed_search_q(search[:SEARCH_MAX_LENGTH]))

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = NotificationListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = NotificationListSerializer(qs, many=True)
        return success_response("Notifications retrieved.", data=serializer.data)

    def retrieve(self, request, pk=None):
        """
        GET /notifications/{id}/ - single in-app notification detail.

        Strictly scoped to the requesting user's IN_APP notifications. Anything
        else (another user's record, an email record, a bad id) returns 404 -
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
        return success_response(
            "Unread count retrieved.",
            data={"unread_count": _unread_in_app_count(request.user)},
        )

    @action(detail=False, methods=["post"], url_path="mark-read")
    def mark_read(self, request):
        """
        POST /notifications/mark-read/
        Mark a list of notification IDs as read. Only the requesting user's
        IN_APP notifications are updated - foreign or EMAIL ids are skipped.
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

    @action(detail=False, methods=["post"], url_path="acknowledge-route")
    def acknowledge_route(self, request):
        """Mark this user's notifications read when their destination is viewed.

        POST /notifications/acknowledge-route/
        Returns ``updated_count`` (rows this call cleared) and ``unread_count``
        (the caller's unread total afterwards). The two routinely disagree,
        which is why both are here: the record's own GET clears its
        notifications as it serves them, so by the time this call lands there
        is often nothing left to update, and ``updated_count`` alone would
        leave the bell badge advertising a notice already read until the next
        poll. The count lets the client correct the badge from a response it
        already makes.

        Only a route naming a record acknowledges anything. A path that names a
        module index clears nothing and reports the count, because arriving at
        an index is no evidence that anything on it was read.
        """
        serializer = AcknowledgeRouteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        route_q = notification_route_q(serializer.validated_data["path"])
        updated = 0
        if route_q is not None:
            updated = Notification.all_objects.filter(
                route_q,
                recipient=request.user,
                channel=ChannelChoices.IN_APP,
                is_read=False,
            ).update(is_read=True, read_at=timezone.now())
        return success_response(
            f"{updated} notification(s) acknowledged.",
            data={
                "updated_count": updated,
                "unread_count": _unread_in_app_count(request.user),
            },
        )


# ---------------------------------------------------------------------------
# 2.  Notification history (admin)
# ---------------------------------------------------------------------------

class NotificationHistoryViewSet(viewsets.GenericViewSet):
    """
    Admin notification history log.

    Every caller, CX staff included, sees EXACTLY their asserted tenant's rows:
    the queryset filters on ``request.tenant`` regardless of user type, so
    tenant-NULL rows never appear. Everyone must supply ≥1 filter. Pass
    ``scope=platform`` to narrow to rows owned by a PLATFORM-kind tenant.

    ``Notification.tenant`` is the RECIPIENT'S tenant, which is what makes this
    filter a real boundary: a message about this school but addressed to
    platform staff is owned by the platform and is not in this log. Do not add
    an ``origin_tenant`` filter here for school-tenant callers - it would hand
    them back exactly the rows this scoping exists to keep out.

    Routes:
        GET /notifications/history/      - paginated log
        GET /notifications/history/{id}/ - full detail record

    docstring-name: Notification history
    """
    permission_classes = [IsAuthenticated, HasRBACPermission]
    rbac_permission = NotificationPermission.AUDIT_ACTIVITY
    pagination_class   = XVSPagination

    def get_queryset(self):
        """
        Base queryset, scoped to the asserted tenant.

        Every caller is hard-scoped to request.tenant (all_objects so the
        include_global manager change can't leak tenant-NULL rows into their
        view). CX staff are not exempt: they see the tenant they asserted.
        """
        user = self.request.user
        # History uses the unscoped manager so platform rows are visible only when allowed.
        # The id tiebreaker keeps pagination stable across bulk-created rows that
        # share a created_at - see NotificationViewSet.get_queryset.
        qs = (
            Notification.all_objects
            .select_related("recipient", "event_type")
            .order_by("-created_at", "id")
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
        search          = (params.get("search") or "").strip()

        # Everyone (including callers who already have the implicit tenant
        # filter) must narrow the log with at least one explicit filter, so
        # nobody can dump a whole tenant's table unfiltered. A search term is a
        # filter like any other.
        if not any([
            scope, recipient_email, event_type_key,
            channel, status_param, created_after, created_before, search,
        ]):
            raise FilterRequiredError()

        if scope == _PLATFORM_SCOPE:
            qs = qs.filter(tenant__kind="PLATFORM")
        if recipient_email:
            qs = qs.filter(recipient__email__icontains=recipient_email)
        if search:
            # Applied to the queryset, so page counts describe the search result.
            term = search[:SEARCH_MAX_LENGTH]
            qs = qs.filter(
                _feed_search_q(term)
                | Q(recipient__email__icontains=term)
                | Q(unregistered_email__icontains=term)
            )
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
        is_vision_staff = getattr(request.user, "is_platform_user", False)
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
    Notification settings - the EFFECTIVE matrix and per-scope overrides.

    GET   /notify/settings/        - full effective matrix for the scope.
    PATCH /notify/settings/update/ - upsert (or, for a branch, remove) override
                                     rows by (event_type_key, channel).

    Scope resolution (same for GET and PATCH) follows the asserted tenant plus an
    optional ``?branch=<id>``; there is no ?school= parameter:
      * Business tenant, no branch → the tenant's own rows, overlaid on the
        platform defaults. Asserting another tenant's ?tenant= slug is refused
        with 404 by the auth layer (this view does not set
        platform_cross_tenant_param), so the no-leak guarantee still holds.
      * Business tenant with ?branch= → that branch's rows, overlaid on the
        tenant's. The branch must belong to the asserted tenant and be one the
        caller may work in (vs_rbac.scoping.visible_branch_ids); anything else
        is the same 404 whether the branch is unknown, foreign or not theirs.
      * PLATFORM-kind tenant (CX staff) → the platform DEFAULT layer, the
        tenant-NULL rows. It cannot target one tenant's rows from here, and the
        platform layer has no branches, so ?branch= is a 404 there.

    Who may change what. The key (communication_permissions.enforce) opens the
    screen; the caller's branch reach decides the scope they may write. A caller
    with whole-tenant reach writes the tenant's rows and any branch's rows. A
    caller whose reach is limited to some branches (a branch admin) writes only
    their own branches' rows: a PATCH at the tenant scope is a 403
    (BRANCH_SCOPE_REQUIRED), and their GET at the tenant scope reports
    ``can_edit: false`` on every row.

    Each row carries ``source`` ("branch" | "tenant" | "platform" | "default"),
    ``branch_scoped`` (the event is sent with a branch, so a branch may set it)
    and ``can_edit`` (this caller may change this row at this scope). A branch
    may set only branch_scoped events; transactional events and the in-app
    channel are never editable, at any scope.

    Permission: communication.communication_permissions.enforce (RBAC).

    docstring-name: Notification settings
    """
    permission_classes = [IsAuthenticated, HasRBACPermission]
    rbac_permission    = NotificationPermission.ENFORCE_PERMISSIONS

    # ── Scope helpers ──────────────────────────────────────────────────────

    def _resolve_scope(self, request):
        """
        Resolve (tenant, branch, whole_reach) for this request.

        ``tenant`` None is the platform DEFAULT layer: a PLATFORM-kind tenant
        (Codex staff) manages the tenant-NULL rows every tenant inherits, since
        codex-tenant rows would be inert for everybody else. ``branch`` is the
        named branch, or None for the tenant scope. ``whole_reach`` says whether
        the caller may act for the whole scope resolved: always true once a
        branch has been accepted, and at the tenant scope only for a caller
        whose reach is the whole tenant
        (:func:`vs_rbac.scoping.caller_reaches_whole_tenant`, the rule every
        shared-record write asks).

        The two ?branch= rules mirror vs_config.services.scopes: the branch must
        live under the asserted tenant, and it must be one the caller is
        entitled to. Every refusal raises the same NotFound, so the parameter
        cannot confirm that another tenant's or another branch's id exists.
        """
        from vs_rbac.scoping import caller_reaches_whole_tenant, visible_branch_ids
        from vs_tenants.references import find_branch_in_tenant

        asserted = request.tenant
        is_platform = getattr(asserted, "kind", None) == "PLATFORM"
        reach = visible_branch_ids(request.user, asserted)
        branch_ref = (request.query_params.get("branch") or "").strip()

        if not branch_ref:
            whole = caller_reaches_whole_tenant(request.user, asserted, visible=reach)
            return (None if is_platform else asserted), None, whole

        branch = None if is_platform else find_branch_in_tenant(asserted, branch_ref)
        if branch is None or (reach is not None and branch.pk not in reach):
            raise NotFound("Notification settings scope not found.")
        return asserted, branch, True

    def _build_matrix(self, tenant, branch=None, *, editable=True):
        """
        Build the effective settings matrix for a scope.

        For each active event type × supported channel, resolve the effective
        value and the layer that produced it. resolve_settings_bulk owns the
        is_active / transactional / layering logic, provenance included, so this
        method only shapes rows and decides ``can_edit``. ``editable`` is whether
        the caller may write at this scope at all. Total cost: 1 event-type
        query + 1 settings query.
        """
        event_types = list(
            NotificationEventType.objects.filter(is_active=True)
            .order_by("source_module", "key")
        )
        rows = settings_rows(event_types, tenant=tenant, branch=branch)
        resolved_by_et = resolve_settings_bulk(
            event_types, tenant=tenant, branch=branch, rows=rows,
        )

        matrix = []
        for et in event_types:
            resolved = resolved_by_et[et.id]
            for channel in et.supported_channels:
                is_enabled, source = resolved.get(channel, (False, SOURCE_DEFAULT))
                can_edit = (
                    editable
                    and not et.is_transactional
                    and channel != ChannelChoices.IN_APP
                    and (branch is None or et.branch_scoped)
                )
                matrix.append({
                    "event_type_key":   et.key,
                    "event_type_label": et.label,
                    "source_module":    et.source_module,
                    "source_module_label": notification_labels.source_module_label(et.source_module),
                    "channel":          channel,
                    "channel_label":    notification_labels.channel_label(channel),
                    "is_enabled":       is_enabled,
                    "is_transactional": et.is_transactional,
                    "source":           source,
                    "source_label":     notification_labels.setting_source_label(source),
                    "branch_scoped":    et.branch_scoped,
                    "can_edit":         can_edit,
                })
        return matrix

    # ── Read ───────────────────────────────────────────────────────────────

    def list(self, request):
        """GET /notify/settings/ - the effective matrix for the scope."""
        tenant, branch, whole_reach = self._resolve_scope(request)
        matrix = self._build_matrix(tenant, branch, editable=whole_reach)
        return success_response("Settings retrieved.", data=matrix)

    # ── Write ──────────────────────────────────────────────────────────────

    @staticmethod
    def _item_error(et, key, channel, is_enabled, branch):
        """The per-item refusal for one update, or None when it may be written."""
        if et is None:
            return (
                NotificationErrorCode.UNKNOWN_EVENT_TYPE,
                f"Unknown or inactive event type: '{key}'.",
            )
        if channel not in ChannelChoices.ALL:
            # Reject unknown channel strings before checking event-specific support.
            return NotificationErrorCode.UNKNOWN_CHANNEL, f"Unknown channel: '{channel}'."
        if channel not in et.supported_channels:
            return (
                NotificationErrorCode.UNSUPPORTED_CHANNEL,
                f"Channel '{channel}' is not supported by '{key}'.",
            )
        if et.is_transactional:
            # Must-send events ignore settings rows, so overrides would be misleading.
            return (
                NotificationErrorCode.TRANSACTIONAL_NOT_CONFIGURABLE,
                f"'{key}' is a transactional event and cannot be "
                "configured - it always dispatches.",
            )
        if branch is not None and not et.branch_scoped:
            # A branch row for an event sent without a branch would never be read.
            return (
                NotificationErrorCode.BRANCH_NOT_CONFIGURABLE,
                f"'{key}' is not sent for a particular branch, so it can only "
                "be set for all branches together.",
            )
        if channel == ChannelChoices.IN_APP and is_enabled is False:
            # Product policy keeps the in-app audit/feed trail always enabled.
            return (
                NotificationErrorCode.IN_APP_ALWAYS_ENABLED,
                "The in-app channel cannot be disabled.",
            )
        if is_enabled is None and branch is None:
            return (
                NotificationErrorCode.RESET_NEEDS_BRANCH,
                "Only a branch's setting can be reset to follow the setting "
                "for all branches. Switch this one on or off instead.",
            )
        return None

    def partial_update(self, request):
        """
        PATCH /notify/settings/update/[?branch=<id>]
        Upsert override rows by (event_type_key, channel) at the resolved scope.
        With a branch, ``is_enabled: null`` removes that branch's row so it
        inherits again. Atomic: one refused item writes nothing.

        Refusals:
          * 404 - the branch is unknown, another tenant's, or not the caller's.
          * 403 BRANCH_SCOPE_REQUIRED - no branch named by a caller whose reach
            is limited to some branches.
          * 400 with per-item errors - unknown event key, unknown or unsupported
            channel, disabling IN_APP (IN_APP_ALWAYS_ENABLED), toggling a
            transactional event (TRANSACTIONAL_NOT_CONFIGURABLE), a branch
            setting an event that is not branch_scoped (BRANCH_NOT_CONFIGURABLE),
            ``null`` without a branch (RESET_NEEDS_BRANCH).
        """
        tenant, branch, whole_reach = self._resolve_scope(request)
        if not whole_reach:
            return error_response(
                "Your access covers only some branches, so you cannot change "
                "notification settings for all branches at once. Choose one of "
                "your branches to change its settings.",
                status=status.HTTP_403_FORBIDDEN,
                code=NotificationErrorCode.BRANCH_SCOPE_REQUIRED,
            )

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
            refusal = self._item_error(
                event_types.get(item["event_type_key"]), item["event_type_key"],
                item["channel"], item["is_enabled"], branch,
            )
            if refusal is not None:
                errors.append({"index": idx, "error_code": refusal[0], "message": refusal[1]})

        if errors:
            return error_response(
                "One or more updates were rejected.",
                error={"updates": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The full PATCH is all-or-nothing so settings cannot partially apply.
        with transaction.atomic():
            layer = NotificationSetting.all_objects.filter(tenant=tenant, branch=branch)
            stored = {
                (row["event_type_id"], row["channel"]): row["is_enabled"]
                for row in layer.filter(
                    event_type__in=event_types.values(),
                ).values("event_type_id", "channel", "is_enabled")
            }
            for item in updates:
                et = event_types[item["event_type_key"]]
                channel, is_enabled = item["channel"], item["is_enabled"]
                if is_enabled is None:
                    layer.filter(event_type=et, channel=channel).delete()
                else:
                    NotificationSetting.all_objects.update_or_create(
                        tenant=tenant,
                        branch=branch,
                        event_type=et,
                        channel=channel,
                        defaults={"is_enabled": is_enabled, "updated_by": request.user},
                    )
                record_setting_change(
                    actor=request.user,
                    tenant=tenant,
                    branch=branch,
                    event_type=et,
                    channel=channel,
                    before=stored.get((et.id, channel)),
                    after=is_enabled,
                )
                stored[(et.id, channel)] = is_enabled

        # Return the updated effective entries (fresh resolve) at the same scope.
        matrix = self._build_matrix(tenant, branch, editable=whole_reach)
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

    GET   /notifications/templates/             - list all templates
    POST  /notifications/templates/             - create template
    GET   /notifications/templates/{id}/        - retrieve single
    PATCH /notifications/templates/{id}/        - update
    GET   /notifications/templates/{id}/preview/- render preview with sample data
    POST  /notifications/templates/{id}/preview/- render preview with own context

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
        """GET /notifications/templates/ - filters: event_type_key, channel, search."""
        qs = self.get_queryset()

        event_type_key = request.query_params.get("event_type_key")
        if event_type_key:
            qs = qs.filter(event_type__key=event_type_key)

        channel = request.query_params.get("channel")
        if channel:
            qs = qs.filter(channel=channel)

        search = (request.query_params.get("search") or "").strip()
        if search:
            term = search[:SEARCH_MAX_LENGTH]
            qs = qs.filter(
                Q(event_type__key__icontains=term)
                | Q(event_type__label__icontains=term)
                | Q(subject__icontains=term)
                | Q(body__icontains=term)
            )

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

        record_template_change(actor=request.user, template=template)
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

        before = template_snapshot(template)
        updated = serializer.save()
        record_template_change(actor=request.user, template=updated, before=before)
        return success_response(
            "Template updated.",
            data=NotificationTemplateSerializer(updated).data,
        )

    @action(detail=False, methods=["get"], url_path="available-events")
    def available_events(self, request):
        """
        GET /notifications/templates/available-events/

        The (event type, channel) pairs that have no template yet - what the
        "new template" screen can actually offer. An event exists only when code
        somewhere fires it, so this is the whole creatable set.
        """
        taken = set(
            NotificationTemplate.objects.values_list("event_type_id", "channel")
        )
        rows = []
        for event_type in NotificationEventType.objects.filter(is_active=True).order_by(
            "source_module", "key",
        ):
            missing = [
                channel for channel in event_type.supported_channels
                if (event_type.id, channel) not in taken
            ]
            if missing:
                rows.append({
                    "event_type":       str(event_type.id),
                    "event_type_key":   event_type.key,
                    "event_type_label": event_type.label,
                    "source_module":    event_type.source_module,
                    "source_module_label": notification_labels.source_module_label(event_type.source_module),
                    "description":      event_type.description,
                    "channels":         missing,
                })
        return success_response("Available events retrieved.", data=rows)

    @action(detail=True, methods=["get", "post"], url_path="preview")
    def preview(self, request, pk=None):
        """
        GET|POST /notifications/templates/{id}/preview/

        Returns the message as a recipient would see it: subject, plain body
        and - for email - the stored HTML rendered with sample data. GET needs
        no payload: sample values are generated from the variables the template
        uses. POST accepts ``{"context": {...}}`` to override any of them, and
        ``{"draft": {...}}`` to preview unsaved editor content (nothing is
        written, which is what makes live editing safe).

        The HTML comes back as a JSON string rather than an HTML response on
        purpose: the console renders it inside a sandboxed iframe, so a preview
        can never execute against the API origin. Nothing is sent and no
        Notification record is created.
        """
        try:
            template = self.get_queryset().get(pk=pk)
        except NotificationTemplate.DoesNotExist:
            return error_response(
                "Template not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = NotificationTemplatePreviewSerializer(
            data=request.data if request.method == "POST" else {},
        )
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

    GET /notifications/event-types/      - list all active event types
    GET /notifications/event-types/{id}/ - retrieve single event type

    docstring-name: Notification event types
    """
    # See MyNotifications above: a bare IsAuthenticated would bypass the
    # pending-tenant surface gate that the project defaults install. No
    # ``pending_tenant_surface`` here on purpose: only the personal inbox was
    # opened to a pending tenant, and the catalogue is not part of it.
    permission_classes = [IsAuthenticated, TenantSurfaceAllowed]

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
