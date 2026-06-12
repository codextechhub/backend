"""
Background-task monitoring for the admin console.

Task results are persisted to PostgreSQL by django-celery-results
(CELERY_RESULT_BACKEND = "django-db"), so the intranet frontend can show
what the Celery worker is doing without anyone reading Render logs.

Endpoints (all CX-staff only):
    GET /v1/admin/tasks/            list task runs (filters below)
    GET /v1/admin/tasks/stats/      counts by status + per-task breakdown
    GET /v1/admin/tasks/schedule/   the beat schedule + execution mode

List filters:
    ?status=SUCCESS|FAILURE|STARTED|PENDING|RETRY|REVOKED
    ?task=<substring of the task name>     e.g. ?task=import
    ?since=YYYY-MM-DD                      created on/after this date
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from django_celery_results.models import TaskResult
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action

from core.pagination import XVSPagination
from core.response import success_response

from .permissions import IsVisionStaff


class TaskResultSerializer(serializers.ModelSerializer):
    runtime_seconds = serializers.SerializerMethodField()

    class Meta:
        model = TaskResult
        fields = [
            "id", "task_id", "task_name", "periodic_task_name", "status",
            "worker", "date_created", "date_started", "date_done",
            "runtime_seconds", "result", "traceback",
        ]

    def get_runtime_seconds(self, obj):
        if obj.date_started and obj.date_done:
            return round((obj.date_done - obj.date_started).total_seconds(), 3)
        return None


class TaskMonitorViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Read-only window onto Celery task history."""

    permission_classes = [IsVisionStaff]
    serializer_class = TaskResultSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = TaskResult.objects.order_by("-date_created")
        params = self.request.query_params

        status_param = (params.get("status") or "").strip().upper()
        if status_param:
            qs = qs.filter(status=status_param)

        task = (params.get("task") or "").strip()
        if task:
            qs = qs.filter(task_name__icontains=task)

        since = (params.get("since") or "").strip()
        if since:
            qs = qs.filter(date_created__date__gte=since)

        return qs

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Status counts (all-time and last 24h) plus a per-task breakdown."""
        day_ago = timezone.now() - timedelta(hours=24)

        by_status = dict(
            TaskResult.objects.values_list("status").annotate(n=Count("id"))
        )
        last_24h = dict(
            TaskResult.objects.filter(date_created__gte=day_ago)
            .values_list("status").annotate(n=Count("id"))
        )
        by_task = list(
            TaskResult.objects.values("task_name")
            .annotate(
                runs=Count("id"),
            )
            .order_by("-runs")[:20]
        )
        failures = list(
            TaskResult.objects.filter(status="FAILURE")
            .order_by("-date_done")
            .values("task_name", "date_done", "task_id")[:5]
        )
        return success_response(
            message="Task statistics retrieved.",
            data={
                "by_status": by_status,
                "last_24h": last_24h,
                "by_task": by_task,
                "recent_failures": failures,
                "total": TaskResult.objects.count(),
            },
        )

    @action(detail=False, methods=["get"])
    def schedule(self, request):
        """The beat schedule as configured in code, plus the execution mode."""
        from apps.celery import app as celery_app

        entries = [
            {
                "name": name,
                "task": entry["task"],
                "schedule": str(entry["schedule"]),
            }
            for name, entry in (celery_app.conf.beat_schedule or {}).items()
        ]
        return success_response(
            message="Beat schedule retrieved.",
            data={
                "eager_mode": bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)),
                "broker_configured": bool(getattr(settings, "CELERY_BROKER_URL", "")),
                "entries": entries,
            },
        )
