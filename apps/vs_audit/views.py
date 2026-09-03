from __future__ import annotations

import csv
import io
import logging
import tempfile
from datetime import timedelta

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db.models import Count, Exists, F, OuterRef, Q, Value
from django.db.models.functions import Concat, TruncDate, TruncHour
from django.http import FileResponse
from django.utils import timezone
from rest_framework import generics
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.response import success_response, error_response
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from vs_tenants.models import Tenant

from .models import (
    AuditEvent,
    EntityAuditTrail,
    AuditExportJob,
    ComplianceRule,
    AuditSeverity,
    AuditStatus,
    AuditActorType,
    AuditModuleKey,
    AuditActionType,
    ExportJobStatus,
    ExportFormat,
)
from .scoping import (
    audit_scope_predicate,
    latest_visible_event_at,
    scope_events_to_caller,
    visible_trail_counters,
)
from .serializers import (
    AuditEventListSerializer,
    AuditEventDetailSerializer,
    EntityAuditTrailSerializer,
    AuditExportJobListSerializer,
    AuditExportJobDetailSerializer,
    ComplianceRuleListSerializer,
    ComplianceRuleDetailSerializer,
    ComplianceRuleCreateUpdateSerializer,
    AuditEventFilterSerializer,
    NO_TENANT,
)

logger = logging.getLogger(__name__)

# How long a produced export stays downloadable.
EXPORT_RETENTION_DAYS = 7

# Columns written by the audit CSV export, in order.
EXPORT_CSV_HEADER = [
    "event_id", "event_at", "module_key", "action_type",
    "severity", "status", "actor_type", "actor_email", "actor_label",
    "entity_type", "entity_id", "entity_label", "summary",
]

# Rows are written and measured in blocks of this size.
_EXPORT_CHUNK = 500


# -----------------------------------------------------------------------------
# Who may read an audit row
# -----------------------------------------------------------------------------
# The predicate itself moved to ``vs_audit.scoping`` so the Export Centre dataset
# can read the same one without importing this module. It is re-exported here
# because every view below already reads it under these names.


def scope_export_jobs_to_caller(queryset, request):
    """Confine an ``AuditExportJob`` queryset to the caller's own tenant.

    ``AuditExportJob`` has no tenant column, so the requester is what bounds it
    - the same reasoning ``AuditExportJobDownloadView`` already documents, one
    step wider. The download stays the requester's own; the history is the
    tenant's, because a school's second audit officer should see that the first
    one took the trail out of the building.

    A job whose requester has since been deleted (``requested_by`` is
    ``SET_NULL``) drops out of a tenant's history rather than into everyone's.
    """
    user = getattr(request, "user", None)
    home = getattr(user, "tenant", None)
    if getattr(home, "kind", None) == Tenant.Kind.PLATFORM:
        return queryset
    tenant = getattr(request, "tenant", None) or home
    if tenant is None:
        return queryset.none()
    return queryset.filter(requested_by__tenant=tenant)


# -----------------------------------------------------------------------------
# Audit Event Views
# -----------------------------------------------------------------------------

def apply_audit_event_filters(queryset, filters):
    """Apply the validated Event Explorer/export filter contract."""

    if module_keys := filters.get("module_key"):
        queryset = queryset.filter(module_key__in=module_keys)
    if action_types := filters.get("action_type"):
        queryset = queryset.filter(action_type__in=action_types)
    if severities := filters.get("severity"):
        queryset = queryset.filter(severity__in=severities)
    if statuses := filters.get("status"):
        queryset = queryset.filter(status__in=statuses)
    if tenant_slug := filters.get("tenant_slug"):
        # ``__none__`` is a first-class answer, not a fallback: platform
        # operations, nightly sweeps and management commands legitimately belong
        # to no customer, and a filter that could only narrow *to* a tenant would
        # make every one of them unreachable from this screen.
        if tenant_slug == NO_TENANT:
            queryset = queryset.filter(tenant__isnull=True)
        else:
            queryset = queryset.filter(tenant__slug=tenant_slug)
    if actor_type := filters.get("actor_type"):
        queryset = queryset.filter(actor_type=actor_type)
    if actor_user_id := filters.get("actor_user_id"):
        queryset = queryset.filter(actor_user_id=actor_user_id)
    if impersonation_session_id := filters.get("impersonation_session_id"):
        queryset = queryset.filter(impersonation_session_id=impersonation_session_id)
    if entity_type := filters.get("entity_type"):
        queryset = queryset.filter(entity_type__iexact=entity_type)
    if entity_id := filters.get("entity_id"):
        queryset = queryset.filter(entity_id=entity_id)
    if date_from := filters.get("date_from"):
        queryset = queryset.filter(event_at__gte=date_from)
    if date_to := filters.get("date_to"):
        queryset = queryset.filter(event_at__lte=date_to)
    if search := filters.get("search"):
        queryset = queryset.annotate(
            _actor_full_name=Concat(
                "actor_user__first_name",
                Value(" "),
                "actor_user__last_name",
            ),
        ).filter(
            Q(summary__icontains=search)
            | Q(entity_label__icontains=search)
            | Q(entity_id__icontains=search)
            | Q(actor_label__icontains=search)
            | Q(action_type__icontains=search)
            | Q(actor_user__email__icontains=search)
            | Q(_actor_full_name__icontains=search)
        )

    return queryset


class AuditEventFilterOptionsView(APIView):
    """Return the authoritative choices used by Event Explorer filters."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def get(self, request):
        def options(choices):
            return [{"value": value, "label": label} for value, label in choices]

        return success_response(
            message="Audit event filter options retrieved.",
            data={
                "modules": options(AuditModuleKey.choices),
                "actions": options(AuditActionType.choices),
                "severities": options(AuditSeverity.choices),
                "statuses": options(AuditStatus.choices),
                "actor_types": options(AuditActorType.choices),
                "tenants": self._tenant_options(request),
            },
        )

    def _tenant_options(self, request):
        """The tenant roster for ``tenant_slug``, and only for platform callers.

        The key that opens this endpoint is ``platform.audit.view``, but a key's
        namespace is not the same thing as its audience: nothing in the grant
        path stops that key being attached to a role inside a school tenant, and
        this app's own export fixture attaches it to one. So the roster is gated
        on the caller's tenant kind, which cannot be granted away, rather than on
        the permission. Bright Star's audit officer opening the filter drawer
        would otherwise be handed the name and slug of every other school on the
        platform - Codex's customer list - which is a disclosure the Explorer
        never made before and is not what "narrow the trail to one tenant" asks
        for. Platform staff already see this list on the schools and proxy
        screens, so for them it is nothing new. The gate stays correct if the
        RBAC layer later forbids such grants outright; it just stops being the
        only thing holding the line.

        An empty list is a complete answer, not a broken one: a single-tenant
        caller has no tenant dimension to offer, exactly as a single-branch
        school gets no branch switcher.
        """
        tenant = getattr(request, "tenant", None)
        if getattr(tenant, "kind", None) != Tenant.Kind.PLATFORM:
            return []
        rows = [
            {"value": slug, "label": name}
            for slug, name in Tenant.objects.order_by("name").values_list("slug", "name")
        ]
        # Platform operations, sweeps and management commands belong to nobody,
        # and they are unreachable from this screen unless the console can offer
        # the sentinel as a choice beside the real tenants.
        rows.append({"value": NO_TENANT, "label": "No tenant (platform-level)"})
        return rows


class AuditEventListView(generics.ListAPIView):
    """
    GET /audit/events/

    Returns paginated audit events.
    Supports filtering with query params.

    docstring-name: Audit events
    """

    serializer_class = AuditEventListSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def list(self, request, *args, **kwargs):
        from rest_framework.response import Response
        from vs_user.models import User

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        events = page if page is not None else list(queryset)

        # Single bulk query to resolve entity users - avoids N+1.
        # User.id is a BigAutoField so we must coerce entity_id to int and
        # discard any values that aren't numeric (e.g. UUID-format entity IDs
        # stored by older audit code), otherwise filter() raises ValueError.
        raw_entity_ids = {e.entity_id for e in events if e.entity_type == "User" and e.entity_id}
        numeric_entity_ids = []
        for eid in raw_entity_ids:
            try:
                numeric_entity_ids.append(int(eid))
            except (ValueError, TypeError):
                pass
        entity_users = {
            str(u.id): u
            for u in User.objects.filter(id__in=numeric_entity_ids).only("id", "first_name", "last_name", "email")
        } if numeric_entity_ids else {}

        ctx = {**self.get_serializer_context(), "entity_users": entity_users}
        serializer = self.get_serializer(events, many=True, context=ctx)

        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def get_queryset(self):
        # ``tenant`` rides along because both event serializers render it as a
        # slug. Now that the column is actually populated, leaving it out
        # turns one query into one-per-row on a paginated screen.
        queryset = AuditEvent.objects.select_related("actor_user", "tenant").all()

        # The boundary comes first, so ``tenant_slug`` below can only ever
        # narrow inside what this caller may already read - never widen it.
        queryset = scope_events_to_caller(queryset, self.request)

        # Validate incoming filters first
        filter_serializer = AuditEventFilterSerializer(data=self.request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        filters = filter_serializer.validated_data

        return apply_audit_event_filters(queryset, filters).order_by("-event_at")


class AuditEventDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """
    GET /audit/events/<uuid:id>/

    Returns one audit event in full detail.

    docstring-name: Audit events
    """

    serializer_class = AuditEventDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"
    lookup_field = "id"

    def get_queryset(self):
        # Same boundary as the list, so another tenant's event is a 404 even
        # when its id is already known - from an export taken before this was
        # fixed, from a log line, or from a shared link.
        return scope_events_to_caller(
            AuditEvent.objects.select_related("actor_user", "tenant").all(),
            self.request,
        )


# -----------------------------------------------------------------------------
# Entity Trail View
# -----------------------------------------------------------------------------

class EntityAuditTrailListView(generics.ListAPIView):
    """
    GET /audit/entity-trails/

    Paginated catalogue of every audited entity.

    docstring-name: Entity audit trails
    """

    serializer_class = EntityAuditTrailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def get_queryset(self):
        qs = EntityAuditTrail.objects.all()

        # ``EntityAuditTrail`` has no tenant column - it is keyed only on
        # (entity_type, entity_id) - so it is bounded by the events behind it:
        # an entity is listed when the caller can read at least one of its
        # events. Without this the catalogue names every audited object on the
        # platform, and ``entity_label`` is a readable name ("Purchase order
        # 00042 for the science block"), so the list alone tells Bright Star
        # what Greenfield has been buying.
        #
        # Deliberately kept separate from the ordering annotation below, which
        # is the same predicate and would give the same set as
        # ``latest_event_at__isnull=False``. Folding the two together would make
        # any future change to how the catalogue is *sorted* a change to who can
        # *see* it, and that is not a trade worth one subquery.
        predicate = audit_scope_predicate(self.request)
        if predicate is not None:
            qs = qs.filter(Exists(
                AuditEvent.objects.filter(predicate).filter(
                    entity_type=OuterRef("entity_type"),
                    entity_id=OuterRef("entity_id"),
                )
            ))

        params = self.request.query_params
        if entity_type := params.get("entity_type"):
            qs = qs.filter(entity_type=entity_type)
        if search := params.get("search"):
            qs = qs.filter(
                Q(entity_id__icontains=search) | Q(entity_label__icontains=search)
            )
        # Ordered on the caller's own most recent event, computed in SQL.
        #
        # This used to sort on a stored ``last_event_at`` column, which is gone:
        # it was part of a rollup that only ever grew, so it could outlive the
        # events it described and put a dead trail at the top of the console.
        # ``latest_event_at`` is the same number the row's ``last_event_at``
        # will show, read off the same rows through the same predicate, so the
        # list is sorted by the column the reader can see.
        #
        # ``nulls_last`` puts trails with no readable event at the end. For a
        # tenant caller the Exists above has already removed them; for a
        # platform caller they are the entities whose events were deleted
        # underneath them - still catalogued, and no longer sorted as though
        # something had just happened to them. ``entity_type``/``entity_id``
        # break ties so paging over equal timestamps cannot repeat or skip a
        # row.
        return qs.annotate(
            latest_event_at=latest_visible_event_at(self.request),
        ).order_by(
            F("latest_event_at").desc(nulls_last=True), "entity_type", "entity_id",
        )

    def list(self, request, *args, **kwargs):
        from rest_framework.response import Response

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        trails = page if page is not None else list(queryset)

        # One grouped query for the whole page, never one per row, and the
        # same for every caller. A stored rollup costs no query and is wrong:
        # it only ever increments, so it counts events since deleted. One query
        # per page is what an honest number costs.
        ctx = {
            **self.get_serializer_context(),
            "visible_counters": visible_trail_counters(trails, request),
        }
        serializer = self.get_serializer(trails, many=True, context=ctx)

        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class MyActivityView(generics.ListAPIView):
    """
    GET /audit/me/activity/

    Audit events where the signed-in user is the actor - used by the
    /me/security/activity self-service page.

    docstring-name: My activity
    """

    serializer_class = AuditEventListSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_queryset(self):
        qs = AuditEvent.objects.select_related("actor_user", "tenant").filter(
            actor_user=self.request.user,
        )

        params = self.request.query_params
        if module_key := params.get("module_key"):
            qs = qs.filter(module_key=module_key)
        if severity := params.get("severity"):
            qs = qs.filter(severity=severity)
        if search := params.get("search"):
            qs = qs.filter(
                Q(summary__icontains=search) | Q(action_type__icontains=search)
            )
        return qs.order_by("-event_at")


class MyActivitySubjectView(generics.ListAPIView):
    """
    GET /audit/me/activity-on-me/

    Audit events where the signed-in user is the target entity
    (entity_type="User", entity_id=<user.id>) and someone else performed the
    action. Powers the "Things done to your account" tab.

    docstring-name: Actions performed on me
    """

    serializer_class = AuditEventListSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_queryset(self):
        user = self.request.user
        qs = AuditEvent.objects.select_related("actor_user", "tenant").filter(
            entity_type="User",
            entity_id=str(user.id),
        ).exclude(actor_user=user)

        params = self.request.query_params
        if module_key := params.get("module_key"):
            qs = qs.filter(module_key=module_key)
        if severity := params.get("severity"):
            qs = qs.filter(severity=severity)
        if search := params.get("search"):
            qs = qs.filter(
                Q(summary__icontains=search) | Q(action_type__icontains=search)
            )
        return qs.order_by("-event_at")


class EntityAuditTrailDetailView(APIView):
    """
    GET /audit/entity-trails/<str:entity_type>/<str:entity_id>/

    docstring-name: Entity audit trails
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def get(self, request, entity_type, entity_id):
        trail_qs = EntityAuditTrail.objects.filter(
            entity_type=entity_type,
            entity_id=entity_id,
        )

        # The whole trail is serialized in one response, so the tenant column
        # is one join here or one query per event.
        #
        # This route is the enumerable one: ``entity_type`` and ``entity_id``
        # are guessable in a way a UUID is not, so ``/entity-trails/User/42/``
        # walked Greenfield's staff one integer at a time. The same boundary as
        # the Explorer applies, and an entity with no event this caller may read
        # is a 404 rather than an empty trail - whether Greenfield audited
        # something is not Bright Star's business either.
        event_qs = scope_events_to_caller(
            AuditEvent.objects.select_related("actor_user", "tenant").filter(
                entity_type=entity_type,
                entity_id=entity_id,
            ),
            request,
        )

        # Materialised once and reused: the rows decide the 404, they are the
        # response body, and they are the counters. Asking exists() first and
        # then selecting the same rows was one query more than this page needs.
        events = list(event_qs.order_by("-event_at"))

        trail = trail_qs.first()
        if not trail or not events:
            return error_response(
                message="No audit trail found for this entity.",
                status=404,
            )

        # The header must agree with the list under it, so it is counted off
        # that list: ``events`` is already scoped to the caller and already in
        # memory, which makes the three numbers free. This is the whole reason
        # a stored rollup was never needed here - the rows are right there.
        #
        # Every caller is answered this way, platform included. Theirs used to
        # come from the stored rollup, so a platform reviewer opening
        # ``User:1`` read a header saying 1690 above a list of 399 events.
        counters = {
            (trail.entity_type, trail.entity_id): {
                "event_count": len(events),
                "first_event_at": min(event.event_at for event in events),
                "last_event_at": max(event.event_at for event in events),
            }
        }

        data = {
            "trail": EntityAuditTrailSerializer(
                trail, context={"visible_counters": counters},
            ).data,
            "events": AuditEventListSerializer(events, many=True).data,
        }

        return success_response(
            message="Audit trail retrieved successfully.",
            data=data,
        )


# -----------------------------------------------------------------------------
# Audit Export Job Views
# -----------------------------------------------------------------------------

class ExportTooLarge(Exception):
    """Raised while writing when the file passes what storage will accept.

    Carries the row count reached, which is how a caller (and the test) can see
    that generation stopped at the checkpoint rather than running to the end.
    """

    def __init__(self, rows_written: int = 0):
        super().__init__(f"Export exceeded the storage ceiling after {rows_written} rows.")
        self.rows_written = rows_written


def audit_export_size_limit() -> int:
    """Largest export the configured storage will actually accept, in bytes.

    ``STORAGES["default"]`` is ``core.storage.DatabaseStorage``, which refuses
    anything over ``MEDIA_DB_MAX_BYTES`` - and refuses it at the very end, after
    the whole file has been generated. The same ceiling is therefore checked
    while writing, so an oversized export is abandoned early and reported in its
    own words rather than as a storage exception.
    """
    from core.storage import MAX_BYTES_DEFAULT

    return getattr(settings, "MEDIA_DB_MAX_BYTES", MAX_BYTES_DEFAULT)


def readable_byte_ceiling(limit: int) -> str:
    """Render a byte ceiling the way an operator reads it."""
    # Sub-megabyte ceilings in bytes: "0 MB" tells the reader nothing.
    if limit >= 1024 * 1024:
        return f"{limit / (1024 * 1024):.0f} MB"
    return f"{limit:,} bytes"


def write_audit_export_file(job, queryset, masked_fields):
    """Write the export through the default storage; return (file_name, name, rows).

    The CSV is streamed to a temporary file rather than held in memory, and the
    value handed back is the **storage name**, the key ``default_storage``
    chose, which is what ``AuditExportJob.file_path`` holds. Nothing about the
    body goes into that column.

    The temp file is opened in BINARY mode: storage backends read the handle
    through ``django.core.files.File`` and write the chunks straight to their
    destination, and a text-mode handle yields ``str`` chunks that
    ``DatabaseStorage`` cannot put in a ``BinaryField``. ``csv`` needs a text
    interface, so a ``TextIOWrapper`` is layered on and detached before the read.
    """
    file_name = f"audit_export_{job.id}.csv"
    size_limit = audit_export_size_limit()
    row_count = 0

    with tempfile.NamedTemporaryFile(suffix=".csv") as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8", newline="", write_through=True)
        try:
            writer = csv.writer(text)
            writer.writerow(EXPORT_CSV_HEADER)
            since_check = 0
            for event in queryset.iterator(chunk_size=_EXPORT_CHUNK):
                actor_email = getattr(event.actor_user, "email", "") if event.actor_user else ""
                summary = event.summary or ""
                if "summary" in masked_fields:
                    summary = "[REDACTED]"
                writer.writerow([
                    str(event.id), event.event_at.isoformat(), event.module_key,
                    event.action_type, event.severity, event.status, event.actor_type,
                    actor_email, event.actor_label or "", event.entity_type,
                    event.entity_id, event.entity_label or "", summary,
                ])
                row_count += 1
                since_check += 1
                if since_check >= _EXPORT_CHUNK:
                    since_check = 0
                    # Stop as soon as the file passes what storage accepts, rather
                    # than generating the rest and failing at save.
                    text.flush()
                    if handle.tell() > size_limit:
                        raise ExportTooLarge(row_count)
            text.flush()
            if handle.tell() > size_limit:
                raise ExportTooLarge(row_count)
        finally:
            # Detach so closing the wrapper never closes the temp file we are
            # about to hand to storage.
            text.detach()

        handle.seek(0)
        storage_name = default_storage.save(
            f"audit-exports/{job.id}/{file_name}", File(handle, name=file_name),
        )

    return file_name, storage_name, row_count


class AuditExportJobListView(generics.ListCreateAPIView):
    """
    GET /audit/exports/   - paginated export history
    POST /audit/exports/  - queue a new CSV export job

    docstring-name: Audit export jobs
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.export"

    def get_queryset(self):
        # ``requested_by`` is rendered by ``UserSlimSerializer``, so an unscoped
        # history handed Bright Star the name and email of every Codex and
        # Greenfield staff member who has ever run an export.
        queryset = scope_export_jobs_to_caller(
            AuditExportJob.objects.select_related("requested_by").all(),
            self.request,
        )

        status_value = self.request.query_params.get("status")

        if status_value:
            queryset = queryset.filter(status=status_value)

        return queryset.order_by("-requested_at")

    def get_serializer_class(self):
        return AuditExportJobListSerializer

    def create(self, request, *args, **kwargs):
        """Synchronously generate a CSV export and persist its metadata."""
        filter_payload = request.data.get("filter_payload") or {}
        export_format = request.data.get("export_format") or ExportFormat.CSV

        if export_format != ExportFormat.CSV:
            return error_response(
                message="Only CSV exports are supported at this time.",
                status=400,
            )

        filter_serializer = AuditEventFilterSerializer(data=filter_payload)
        filter_serializer.is_valid(raise_exception=True)
        normalized_filter_payload = filter_serializer.validated_data

        # Build exports through exactly the same validated filter path as the
        # list - and the same boundary. This is the copy that leaves the
        # building, so it must not be able to carry rows the screen would not
        # have shown.
        qs = apply_audit_event_filters(
            scope_events_to_caller(
                AuditEvent.objects.select_related("actor_user").all(), request,
            ),
            normalized_filter_payload,
        ).order_by("-event_at")

        # Apply masking from active rules (mirror what the UI hides).
        active_masking = list(
            ComplianceRule.objects.filter(rule_type="MASKING", is_active=True)
            .values_list("masking_fields", flat=True)
        )
        masked_fields = {field for rule in active_masking for field in (rule or [])}

        job = AuditExportJob.objects.create(
            requested_by=request.user,
            export_format=ExportFormat.CSV,
            filter_payload=filter_payload,
            status=ExportJobStatus.RUNNING,
            started_at=timezone.now(),
        )

        # A job that dies mid-write must not sit at RUNNING for ever, so every
        # failure below ends in FAILED with a reason the requester can act on.
        try:
            file_name, storage_name, rows = write_audit_export_file(job, qs, masked_fields)
        except ExportTooLarge:
            ceiling = readable_byte_ceiling(audit_export_size_limit())
            reason = (
                f"This export is larger than the {ceiling} file limit. "
                "Narrow the date range or filters and try again."
            )
            job.mark_failed(reason)
            return error_response(
                message=reason,
                error={"job_id": str(job.id), "status": job.status},
                status=413,
            )
        except Exception:
            logger.exception("Audit export %s failed", job.id)
            reason = "The export could not be generated. Try again or narrow the filters."
            job.mark_failed(reason)
            return error_response(
                message=reason,
                error={"job_id": str(job.id), "status": job.status},
                status=500,
            )

        # ``file_path`` holds the storage key the default storage chose, which is
        # what its help text has always said it holds. The body lives in storage.
        job.mark_completed(
            row_count=rows,
            file_name=file_name,
            file_path=storage_name,
            expires_in_days=EXPORT_RETENTION_DAYS,
        )

        return success_response(
            message="Export job completed.",
            data=AuditExportJobDetailSerializer(job).data,
            status=201,
        )


class AuditDashboardSummaryView(APIView):
    """
    GET /audit/dashboard-summary/

    Aggregated metrics that power the Security Dashboard:
      - kpis: active sessions, events / critical / failed in the last 24h, locked accounts, active impersonations
      - severity_series: daily INFO/WARNING/CRITICAL counts for the last 14 days
      - module_breakdown: per-module event counts in the last 24h
      - signin_series: SUCCESS vs FAIL login attempt counts per day for the last 30 days
      - critical_heatmap: hour-of-day x day-of-week grid for the last 30 days

    docstring-name: Audit dashboard summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def get(self, request):
        from vs_user.models import LoginSession, AuthAttempt, AccountLockout
        from vs_admin_console.models import ImpersonationSession

        now = timezone.now()
        last_24h = now - timedelta(hours=24)
        last_14d = now - timedelta(days=14)
        last_30d = now - timedelta(days=30)

        # Every series below is an aggregate over the same rows the Event
        # Explorer lists, so it answers to the same boundary. A count is still
        # a disclosure: an unscoped critical_heatmap tells Bright Star which
        # nights Greenfield has incidents.
        #
        # ``LoginSession`` and ``AuthAttempt`` are not filtered here because
        # their default manager is ``TenantAwareManager``, which already applies
        # the request's tenant. ``AccountLockout`` has no tenant column at all,
        # so it goes through its user.
        predicate = audit_scope_predicate(request)
        events = AuditEvent.objects.all()
        lockouts = AccountLockout.objects.all()
        impersonations = ImpersonationSession.objects.all()
        if predicate is not None:
            scope_tenant = getattr(request, "tenant", None) or request.user.tenant
            events = events.filter(predicate)
            lockouts = lockouts.filter(user__tenant=scope_tenant)
            impersonations = impersonations.filter(tenant=scope_tenant)

        events_24h = events.filter(event_at__gte=last_24h)

        kpis = {
            "active_sessions": LoginSession.objects.filter(is_active=True).count(),
            "events_24h": events_24h.count(),
            "critical_24h": events_24h.filter(severity=AuditSeverity.CRITICAL).count(),
            "failed_denied_24h": events_24h.filter(
                status__in=[AuditStatus.FAILED, AuditStatus.DENIED]
            ).count(),
            "locked_accounts": lockouts.filter(locked_until__gt=now).count(),
            "active_impersonations": impersonations.filter(status="ACTIVE").count(),
        }

        # Daily severity rollup for the last 14 days
        severity_rows = (
            events.filter(event_at__gte=last_14d)
            .annotate(day=TruncDate("event_at"))
            .values("day", "severity")
            .annotate(count=Count("id"))
        )
        severity_map: dict[str, dict[str, int]] = {}
        for row in severity_rows:
            key = row["day"].isoformat()
            severity_map.setdefault(key, {"INFO": 0, "WARNING": 0, "CRITICAL": 0})
            severity_map[key][row["severity"]] = row["count"]
        severity_series = [
            {"date": day, **counts}
            for day, counts in sorted(severity_map.items())
        ]

        # Module breakdown over the last 24h
        module_rows = (
            events_24h.values("module_key")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        module_breakdown = [
            {"module_key": row["module_key"], "count": row["count"]}
            for row in module_rows
        ]

        # Sign-in success vs failure for the last 30 days
        signin_rows = (
            AuthAttempt.objects.filter(created_at__gte=last_30d)
            .annotate(day=TruncDate("created_at"))
            .values("day", "result")
            .annotate(count=Count("id"))
        )
        signin_map: dict[str, dict[str, int]] = {}
        for row in signin_rows:
            key = row["day"].isoformat()
            signin_map.setdefault(key, {"SUCCESS": 0, "FAIL": 0})
            bucket = "SUCCESS" if row["result"] == "SUCCESS" else "FAIL"
            signin_map[key][bucket] += row["count"]
        signin_series = [
            {"date": day, **counts}
            for day, counts in sorted(signin_map.items())
        ]

        # Critical event heatmap: hour x weekday for last 30 days
        critical_qs = events.filter(
            event_at__gte=last_30d, severity=AuditSeverity.CRITICAL
        )
        grid = [[0] * 24 for _ in range(7)]
        for event in critical_qs.only("event_at"):
            local = timezone.localtime(event.event_at)
            grid[local.weekday()][local.hour] += 1

        return success_response(
            message="Dashboard summary retrieved.",
            data={
                "kpis": kpis,
                "severity_series": severity_series,
                "module_breakdown": module_breakdown,
                "signin_series": signin_series,
                "critical_heatmap": grid,
                "generated_at": now.isoformat(),
            },
        )


class AuditExportJobDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """
    GET /audit/exports/<uuid:id>/

    docstring-name: Audit export jobs
    """

    serializer_class = AuditExportJobDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.export"
    lookup_field = "id"

    def get_queryset(self):
        # Same boundary as the history list. The download below is narrower
        # still - the requester's own - so a job visible here may legitimately
        # answer 404 on its file.
        return scope_export_jobs_to_caller(
            AuditExportJob.objects.select_related("requested_by").all(),
            self.request,
        )


class AuditExportJobDownloadView(APIView):
    """
    GET /audit/exports/<uuid:id>/download/ - the CSV, if it is yours.

    Audit exports get their own route rather than riding ``/media/<name>``.
    ``core.views.MediaView`` decides by the file's tenant and by the policy its
    owning record registers, and an audit CSV has neither: it is written by a
    background job with no tenant in context, and it answers to the *filters*
    that produced it rather than to any one row. A file that cannot name its
    owner is one ``MediaView`` refuses outright, which is the correct answer
    there and a useless one here.

    So this route asks two questions ``MediaView`` cannot:

    * does the caller hold ``platform.audit.export`` - the same key the Event
      Explorer's own export surface requires; and
    * is this the caller's own job? ``AuditExportJob`` has no tenant column, so
      the requester is what bounds it, and a requester belongs to exactly one
      tenant. Somebody else's export is a 404, not a 403: whether another
      tenant ran an export at all is not this caller's business.

    Expiry and a missing file are answered here too, because both are true of
    the file rather than of the caller.

    docstring-name: Audit export jobs
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.export"

    def get(self, request, id):
        job = AuditExportJob.objects.filter(id=id, requested_by=request.user).first()
        if job is None:
            return error_response(message="Export job not found.", status=404)
        if job.status != ExportJobStatus.COMPLETED:
            return error_response(
                message="This export is not ready to download.",
                status=409,
            )
        if job.is_expired:
            return error_response(
                message="This export file has expired. Run the export again.",
                status=410,
            )
        if not job.file_path or not default_storage.exists(job.file_path):
            return error_response(
                message="The export file is no longer available.",
                status=404,
            )
        return FileResponse(
            default_storage.open(job.file_path, "rb"),
            as_attachment=True,
            filename=job.file_name or f"audit_export_{job.id}.csv",
            content_type="text/csv; charset=utf-8",
        )


# -----------------------------------------------------------------------------
# Compliance Rule Views
# -----------------------------------------------------------------------------

class ComplianceRuleListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    GET /audit/compliance-rules/
    POST /audit/compliance-rules/

    List all rules or create a new one.

    docstring-name: Compliance rules
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.manage"

    def get_queryset(self):
        queryset = ComplianceRule.objects.select_related("tenant").all()

        i_slug = self.request.query_params.get("i_slug")
        rule_type = self.request.query_params.get("rule_type")
        is_active = self.request.query_params.get("is_active")
        module_key = self.request.query_params.get("module_key")

        if i_slug:
            queryset = queryset.filter(tenant__slug=i_slug)

        if rule_type:
            queryset = queryset.filter(rule_type=rule_type)

        if is_active is not None:
            if is_active.lower() == "true":
                queryset = queryset.filter(is_active=True)
            elif is_active.lower() == "false":
                queryset = queryset.filter(is_active=False)

        if module_key:
            queryset = queryset.filter(module_key=module_key)

        return queryset.order_by("name")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ComplianceRuleCreateUpdateSerializer
        return ComplianceRuleListSerializer


class ComplianceRuleDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET /audit/compliance-rules/<uuid:id>/
    PUT /audit/compliance-rules/<uuid:id>/
    PATCH /audit/compliance-rules/<uuid:id>/
    DELETE /audit/compliance-rules/<uuid:id>/

    docstring-name: Compliance rules
    """

    queryset = ComplianceRule.objects.select_related("tenant").all()
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.manage"
    lookup_field = "id"

    def get_serializer_class(self):
        if self.request.method in ["PUT", "PATCH"]:
            return ComplianceRuleCreateUpdateSerializer
        return ComplianceRuleDetailSerializer
