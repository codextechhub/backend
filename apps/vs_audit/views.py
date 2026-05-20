from __future__ import annotations

import csv
import io
from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone
from rest_framework import generics
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.response import success_response, error_response
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission

from .models import (
    AuditEvent,
    EntityAuditTrail,
    AuditExportJob,
    ComplianceRule,
    AuditSeverity,
    AuditStatus,
    ExportJobStatus,
    ExportFormat,
)
from .serializers import (
    AuditEventListSerializer,
    AuditEventDetailSerializer,
    EntityAuditTrailSerializer,
    EntityAuditTrailDetailSerializer,
    AuditExportJobListSerializer,
    AuditExportJobDetailSerializer,
    ComplianceRuleListSerializer,
    ComplianceRuleDetailSerializer,
    ComplianceRuleCreateUpdateSerializer,
    AuditEventFilterSerializer,
)


# -----------------------------------------------------------------------------
# Audit Event Views
# -----------------------------------------------------------------------------

class AuditEventListView(generics.ListAPIView):
    """
    GET /audit/events/

    Returns paginated audit events.
    Supports filtering with query params.
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

        # Single bulk query to resolve entity users — avoids N+1.
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
        queryset = AuditEvent.objects.select_related("actor_user").all()

        # Validate incoming filters first
        filter_serializer = AuditEventFilterSerializer(data=self.request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        filters = filter_serializer.validated_data

        i_slug = filters.get("i_slug")
        module_key = filters.get("module_key")
        action_type = filters.get("action_type")
        severity = filters.get("severity")
        status = filters.get("status")
        actor_type = filters.get("actor_type")
        actor_user_id = filters.get("actor_user_id")
        entity_type = filters.get("entity_type")
        entity_id = filters.get("entity_id")
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        search = filters.get("search")

        if module_key:
            queryset = queryset.filter(module_key=module_key)

        if action_type:
            queryset = queryset.filter(action_type=action_type)

        if severity:
            queryset = queryset.filter(severity=severity)

        if status:
            queryset = queryset.filter(status=status)

        if actor_type:
            queryset = queryset.filter(actor_type=actor_type)

        if actor_user_id:
            queryset = queryset.filter(actor_user_id=actor_user_id)

        if entity_type:
            queryset = queryset.filter(entity_type=entity_type)

        if entity_id:
            queryset = queryset.filter(entity_id=entity_id)

        if date_from:
            queryset = queryset.filter(event_at__gte=date_from)

        if date_to:
            queryset = queryset.filter(event_at__lte=date_to)

        if search:
            queryset = queryset.filter(
                Q(summary__icontains=search) |
                Q(entity_label__icontains=search) |
                Q(entity_id__icontains=search) |
                Q(actor_label__icontains=search)
            )

        return queryset.order_by("-event_at")


class AuditEventDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """
    GET /audit/events/<uuid:id>/

    Returns one audit event in full detail.
    """

    queryset = AuditEvent.objects.select_related(
        "actor_user",
    ).all()
    serializer_class = AuditEventDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"
    lookup_field = "id"


# -----------------------------------------------------------------------------
# Entity Trail View
# -----------------------------------------------------------------------------

class EntityAuditTrailListView(generics.ListAPIView):
    """
    GET /audit/entity-trails/

    Paginated catalogue of every audited entity.
    """

    serializer_class = EntityAuditTrailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def get_queryset(self):
        qs = EntityAuditTrail.objects.all()
        params = self.request.query_params
        if entity_type := params.get("entity_type"):
            qs = qs.filter(entity_type=entity_type)
        if search := params.get("search"):
            qs = qs.filter(
                Q(entity_id__icontains=search) | Q(entity_label__icontains=search)
            )
        return qs.order_by("-last_event_at")


class MyActivityView(generics.ListAPIView):
    """
    GET /audit/me/activity/

    Audit events where the signed-in user is the actor — used by the
    /me/security/activity self-service page.
    """

    serializer_class = AuditEventListSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_queryset(self):
        qs = AuditEvent.objects.select_related("actor_user").filter(actor_user=self.request.user)

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
    """

    serializer_class = AuditEventListSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_queryset(self):
        user = self.request.user
        qs = AuditEvent.objects.select_related("actor_user").filter(
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
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.view"

    def get(self, request, entity_type, entity_id):
        trail_qs = EntityAuditTrail.objects.filter(
            entity_type=entity_type,
            entity_id=entity_id,
        )

        event_qs = AuditEvent.objects.select_related(
            "actor_user",
        ).filter(
            entity_type=entity_type,
            entity_id=entity_id,
        )

        trail = trail_qs.first()
        if not trail:
            return error_response(
                message="No audit trail found for this entity.",
                status=404,
            )

        data = {
            "trail": EntityAuditTrailSerializer(trail).data,
            "events": AuditEventListSerializer(
                event_qs.order_by("-event_at"),
                many=True,
            ).data,
        }

        serializer = EntityAuditTrailDetailSerializer(data)
        return success_response(
            message="Audit trail retrieved successfully.",
            data=serializer.data,
        )


# -----------------------------------------------------------------------------
# Audit Export Job Views
# -----------------------------------------------------------------------------

class AuditExportJobListView(generics.ListCreateAPIView):
    """
    GET /audit/exports/   - paginated export history
    POST /audit/exports/  - queue a new CSV export job
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.export"

    def get_queryset(self):
        queryset = AuditExportJob.objects.select_related(
            "requested_by",
        ).all()

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

        # Build the event queryset using the saved filter payload.
        qs = AuditEvent.objects.select_related("actor_user").all()

        def _get(key):
            value = filter_payload.get(key)
            return value if value not in (None, "", []) else None

        if (module_key := _get("module_key")):
            qs = qs.filter(module_key__in=module_key if isinstance(module_key, list) else [module_key])
        if (action_type := _get("action_type")):
            qs = qs.filter(action_type__in=action_type if isinstance(action_type, list) else [action_type])
        if (severity := _get("severity")):
            qs = qs.filter(severity__in=severity if isinstance(severity, list) else [severity])
        if (status_val := _get("status")):
            qs = qs.filter(status__in=status_val if isinstance(status_val, list) else [status_val])
        if (actor_type := _get("actor_type")):
            qs = qs.filter(actor_type=actor_type)
        if (entity_type := _get("entity_type")):
            qs = qs.filter(entity_type=entity_type)
        if (entity_id := _get("entity_id")):
            qs = qs.filter(entity_id=entity_id)
        if (date_from := _get("date_from")):
            qs = qs.filter(event_at__gte=date_from)
        if (date_to := _get("date_to")):
            qs = qs.filter(event_at__lte=date_to)

        qs = qs.order_by("-event_at")

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

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "event_id", "event_at", "module_key", "action_type",
            "severity", "status", "actor_type", "actor_email", "actor_label",
            "entity_type", "entity_id", "entity_label", "summary",
        ])
        rows = 0
        for event in qs.iterator():
            actor_email = getattr(event.actor_user, "email", "") if event.actor_user else ""
            summary = event.summary or ""
            if "summary" in masked_fields:
                summary = "[REDACTED]"
            writer.writerow([
                str(event.id), event.event_at.isoformat(), event.module_key, event.action_type,
                event.severity, event.status, event.actor_type, actor_email, event.actor_label or "",
                event.entity_type, event.entity_id, event.entity_label or "", summary,
            ])
            rows += 1

        file_name = f"audit_export_{job.id}.csv"
        job.mark_completed(
            row_count=rows,
            file_name=file_name,
            file_path=buffer.getvalue(),  # inline CSV body so the frontend can download it
            expires_in_days=7,
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

        events_24h = AuditEvent.objects.filter(event_at__gte=last_24h)

        kpis = {
            "active_sessions": LoginSession.objects.filter(is_active=True).count(),
            "events_24h": events_24h.count(),
            "critical_24h": events_24h.filter(severity=AuditSeverity.CRITICAL).count(),
            "failed_denied_24h": events_24h.filter(
                status__in=[AuditStatus.FAILED, AuditStatus.DENIED]
            ).count(),
            "locked_accounts": AccountLockout.objects.filter(locked_until__gt=now).count(),
            "active_impersonations": ImpersonationSession.objects.filter(status="ACTIVE").count(),
        }

        # Daily severity rollup for the last 14 days
        severity_rows = (
            AuditEvent.objects.filter(event_at__gte=last_14d)
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
        critical_qs = (
            AuditEvent.objects.filter(
                event_at__gte=last_30d, severity=AuditSeverity.CRITICAL
            )
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
    """

    queryset = AuditExportJob.objects.select_related(
        "requested_by",
    ).all()
    serializer_class = AuditExportJobDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.export"
    lookup_field = "id"


# -----------------------------------------------------------------------------
# Compliance Rule Views
# -----------------------------------------------------------------------------

class ComplianceRuleListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    GET /audit/compliance-rules/
    POST /audit/compliance-rules/

    List all rules or create a new one.
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.manage"

    def get_queryset(self):
        queryset = ComplianceRule.objects.select_related("school").all()

        i_slug = self.request.query_params.get("i_slug")
        rule_type = self.request.query_params.get("rule_type")
        is_active = self.request.query_params.get("is_active")
        module_key = self.request.query_params.get("module_key")

        if i_slug:
            queryset = queryset.filter(school__slug=i_slug)

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
    """

    queryset = ComplianceRule.objects.select_related("school").all()
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.audit.manage"
    lookup_field = "id"

    def get_serializer_class(self):
        if self.request.method in ["PUT", "PATCH"]:
            return ComplianceRuleCreateUpdateSerializer
        return ComplianceRuleDetailSerializer