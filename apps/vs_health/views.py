"""API views for VIGIL (vs_health).

Two flavours:
  * **Analytics endpoints** (APIView) return pre-computed dicts from
    ``vs_health.services`` wrapped in the standard success envelope.
  * **CRUD endpoints** (generics) for incidents, alert rules and deployments
    reuse the core envelope mixins + XVSPagination.

This is a platform/SRE tool: reads require ``platform.health.view`` and writes
require ``platform.health.manage``. Tenant Health reads cross-tenant aggregates,
so it is intentionally gated by the platform permission only.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework import generics
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin
from core.response import success_response, error_response
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission

from . import services
from .constants import PERM_VIEW, PERM_MANAGE
from .models import (
    MonitoredService,
    Incident,
    IncidentEvent,
    AlertRule,
    Alert,
    Deployment,
)
from .serializers import (
    IncidentListSerializer,
    IncidentDetailSerializer,
    IncidentCreateUpdateSerializer,
    IncidentEventCreateSerializer,
    AlertRuleSerializer,
    AlertSerializer,
    DeploymentSerializer,
    TaskRowSerializer,
)

PERMS = [IsAuthenticatedAndActive & HasRBACPermission]


class HealthViewMixin:
    """Read-only health view: requires platform.health.view."""
    permission_classes = PERMS
    rbac_permission = PERM_VIEW


class HealthWriteMixin:
    """Read with view perm, write with manage perm (method-aware)."""
    permission_classes = PERMS

    @property
    def rbac_permission(self):
        method = getattr(getattr(self, "request", None), "method", "GET")
        return PERM_VIEW if method in SAFE_METHODS else PERM_MANAGE


def _range(request):
    return services.parse_range(request.query_params.get("range"))


def _school_id(request):
    raw = request.query_params.get("school")
    if raw in (None, "", "all"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Command Center
# ---------------------------------------------------------------------------

class OverviewView(HealthViewMixin, APIView):
    """GET /health/overview/ — the single-pane-of-glass Command Center payload.

    docstring-name: Command Center overview
    """

    def get(self, request):
        tr = _range(request)
        school_id = _school_id(request)
        deployments = list(
            Deployment.objects.filter(deployed_at__gte=tr.start, deployed_at__lt=tr.end)
            .values("id", "version", "kind", "actor", "text", "deployed_at")
        )
        for d in deployments:
            d["id"] = str(d["id"])
            d["deployed_at"] = d["deployed_at"].isoformat()
        active_incidents = IncidentListSerializer(
            Incident.objects.filter(~Q(status=Incident.Status.RESOLVED))
            .prefetch_related("services")[:10],
            many=True,
        ).data
        data = {
            "range": tr.key,
            "posture": services.overall_posture(),
            "global_uptime": services.global_uptime(),
            "kpis": services.golden_signals(tr, school_id),
            "services": services.service_grid(),
            "request_series": services.request_series(tr, school_id),
            "deployments": deployments,
            "queues": services.queue_overview(),
            "active_incidents": active_incidents,
        }
        return success_response("Overview retrieved successfully.", data)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

class ServiceListView(HealthViewMixin, APIView):
    """GET /health/services/ — worst-first service grid."""

    def get(self, request):
        return success_response("Services retrieved successfully.",
                                {"services": services.service_grid()})


class ServiceDetailView(HealthViewMixin, APIView):
    """GET /health/services/{key}/ — drill-down for one service."""

    def get(self, request, key):
        svc = MonitoredService.objects.filter(key=key).first()
        if not svc:
            return error_response("Service not found.", status=404)
        monitors = {m["key"]: m for m in services.uptime_monitors()}
        recent_alerts = AlertSerializer(
            Alert.objects.filter(service=svc).order_by("-fired_at")[:10], many=True).data
        data = {
            "key": svc.key, "name": svc.name, "group": svc.group, "tier": svc.tier,
            "kind": svc.kind, "status": svc.current_status,
            "uptime": monitors.get(svc.key),
            "recent_alerts": recent_alerts,
        }
        return success_response("Service retrieved successfully.", data)


# ---------------------------------------------------------------------------
# Uptime & Availability
# ---------------------------------------------------------------------------

class UptimeMonitorsView(HealthViewMixin, APIView):
    """GET /health/uptime/monitors/ — uptime bars, response charts, SSL, table."""

    def get(self, request):
        return success_response("Uptime monitors retrieved successfully.",
                                {"monitors": services.uptime_monitors()})


class UptimeMonitorDetailView(HealthViewMixin, APIView):
    """GET /health/uptime/monitors/{key}/ — one monitor."""

    def get(self, request, key):
        monitor = next((m for m in services.uptime_monitors() if m["key"] == key), None)
        if not monitor:
            return error_response("Monitor not found.", status=404)
        return success_response("Monitor retrieved successfully.", monitor)


# ---------------------------------------------------------------------------
# API & Endpoint Health
# ---------------------------------------------------------------------------

class ApiEndpointsView(HealthViewMixin, APIView):
    """GET /health/api-endpoints/ — endpoint table + top-5 cards + code series."""

    def get(self, request):
        tr = _range(request)
        rows = services.endpoint_stats(tr, _school_id(request))
        slowest = sorted(rows, key=lambda r: r["p95"], reverse=True)[:5]
        errored = sorted(rows, key=lambda r: r["error_rate"], reverse=True)[:5]
        data = {
            "range": tr.key,
            "endpoints": rows,
            "top_slowest": slowest,
            "top_errors": errored,
            "status_code_series": services.request_series(tr, _school_id(request)),
        }
        return success_response("Endpoints retrieved successfully.", data)


class ApiEndpointDetailView(HealthViewMixin, APIView):
    """GET /health/api-endpoints/detail/?route=... — endpoint drill-down drawer."""

    def get(self, request):
        route = request.query_params.get("route")
        if not route:
            return error_response("A 'route' query parameter is required.", status=400)
        tr = _range(request)
        return success_response("Endpoint detail retrieved successfully.",
                                services.endpoint_detail(tr, route))


# ---------------------------------------------------------------------------
# Background Jobs & Queues
# ---------------------------------------------------------------------------

class QueuesView(HealthViewMixin, APIView):
    """GET /health/queues/ — queue cards, depth trend, worker pool."""

    def get(self, request):
        return success_response("Queues retrieved successfully.", services.queue_overview())


class TaskListView(HealthViewMixin, generics.ListAPIView):
    """GET /health/tasks/ — the task table (reads core.BackgroundJob).

    Filters: ?status=, ?queue=, ?school=, ?kind=.
    """
    serializer_class = TaskRowSerializer

    def get_queryset(self):
        from core.models import BackgroundJob
        from .tasks import KIND_TO_QUEUE

        qs = BackgroundJob.objects.select_related("school").all()
        params = self.request.query_params
        status = params.get("status")
        if status:
            qs = qs.filter(status=status.upper())
        school = params.get("school")
        if school and school != "all":
            qs = qs.filter(school_id=school)
        kind = params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        queue = params.get("queue")
        if queue:
            kinds = [k for k, v in KIND_TO_QUEUE.items() if v == queue]
            qs = qs.filter(kind__in=kinds) if kinds else qs.filter(kind="__none__")
        return qs.order_by("-created_at")


# ---------------------------------------------------------------------------
# Incidents & Alerts
# ---------------------------------------------------------------------------

class IncidentListCreateView(CreateModelMixin, HealthWriteMixin, generics.ListCreateAPIView):
    """GET (list) / POST (open) incidents."""

    def get_queryset(self):
        qs = Incident.objects.prefetch_related("services").all()
        status = self.request.query_params.get("status")
        if status == "active":
            from django.db.models import Q
            qs = qs.filter(~Q(status=Incident.Status.RESOLVED))
        elif status:
            qs = qs.filter(status=status)
        sev = self.request.query_params.get("severity")
        if sev:
            qs = qs.filter(severity=sev)
        return qs

    def get_serializer_class(self):
        return IncidentCreateUpdateSerializer if self.request.method == "POST" else IncidentListSerializer


class IncidentDetailView(RetrieveModelMixin, UpdateModelMixin, HealthWriteMixin,
                         generics.RetrieveUpdateAPIView):
    """GET / PATCH a single incident (war-room view)."""
    queryset = Incident.objects.prefetch_related("services", "timeline").all()
    lookup_field = "id"

    def get_serializer_class(self):
        return IncidentCreateUpdateSerializer if self.request.method in ("PUT", "PATCH") \
            else IncidentDetailSerializer


class IncidentEventCreateView(HealthWriteMixin, APIView):
    """POST /health/incidents/{id}/events/ — append a timeline update."""

    def post(self, request, id):
        incident = Incident.objects.filter(id=id).first()
        if not incident:
            return error_response("Incident not found.", status=404)
        serializer = IncidentEventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event = IncidentEvent.objects.create(incident=incident, **serializer.validated_data)
        from .serializers import IncidentEventSerializer
        return success_response("Timeline updated.", IncidentEventSerializer(event).data, status=201)


class ReliabilityView(HealthViewMixin, APIView):
    """GET /health/incidents/reliability/ — MTTA/MTTR/counts."""

    def get(self, request):
        return success_response("Reliability stats retrieved successfully.",
                                services.reliability_stats())


class AlertListView(HealthViewMixin, generics.ListAPIView):
    """GET /health/alerts/ — firing alerts (?status=resolved for history)."""
    serializer_class = AlertSerializer

    def get_queryset(self):
        status = self.request.query_params.get("status", "firing")
        qs = Alert.objects.select_related("rule", "service")
        if status in ("firing", "resolved"):
            qs = qs.filter(status=status)
        return qs.order_by("-fired_at")


class AlertRuleListCreateView(CreateModelMixin, HealthWriteMixin, generics.ListCreateAPIView):
    """GET (list) / POST (create) alert rules."""
    serializer_class = AlertRuleSerializer
    queryset = AlertRule.objects.select_related("target_service").all()


class AlertRuleDetailView(RetrieveModelMixin, UpdateModelMixin, HealthWriteMixin,
                          generics.RetrieveUpdateAPIView):
    """GET / PATCH a rule (toggle is_enabled)."""
    serializer_class = AlertRuleSerializer
    queryset = AlertRule.objects.select_related("target_service").all()
    lookup_field = "id"


# ---------------------------------------------------------------------------
# Tenant Health
# ---------------------------------------------------------------------------

class TenantListView(HealthViewMixin, APIView):
    """GET /health/tenants/ — per-institution health grid + noisy-neighbour."""

    def get(self, request):
        tr = _range(request)
        return success_response("Tenant health retrieved successfully.",
                                {"range": tr.key, "tenants": services.tenant_stats(tr)})


class TenantDetailView(HealthViewMixin, APIView):
    """GET /health/tenants/{school_id}/ — golden signals scoped to one tenant."""

    def get(self, request, school_id):
        tr = _range(request)
        try:
            sid = int(school_id)
        except (TypeError, ValueError):
            return error_response("Invalid school id.", status=400)
        data = {
            "range": tr.key,
            "school_id": sid,
            "kpis": services.golden_signals(tr, sid),
            "series": services.request_series(tr, sid),
            "endpoints": services.endpoint_stats(tr, sid),
        }
        return success_response("Tenant health retrieved successfully.", data)


# ---------------------------------------------------------------------------
# Deployments & SLOs
# ---------------------------------------------------------------------------

class DeploymentListCreateView(CreateModelMixin, HealthWriteMixin, generics.ListCreateAPIView):
    """GET (list) / POST (annotate) deployments."""
    serializer_class = DeploymentSerializer
    queryset = Deployment.objects.all()


class SLOView(HealthViewMixin, APIView):
    """GET /health/slos/ — SLO attainment + error budgets."""

    def get(self, request):
        return success_response("SLOs retrieved successfully.", {"slos": services.slo_status()})
