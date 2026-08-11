"""Shared filtering and background-file generation for configuration audit."""

from __future__ import annotations

import csv
import json
import logging
import tempfile
from datetime import timedelta

from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from ..models import ConfigurationAuditEvent, ConfigurationAuditExportJob
from ..serializers import build_configuration_target_labels
from .audit import record_configuration_event


logger = logging.getLogger(__name__)
EXPORT_ROW_LIMIT = 250_000
EXPORT_RETENTION_DAYS = 7
CONCURRENT_EXPORT_LIMIT = 3


def audit_filter_snapshot(validated_filters):
    """Convert validated filter values into JSON-safe, task-stable values."""
    snapshot = {}
    for key, value in validated_filters.items():
        if value in (None, ""):
            continue
        snapshot[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return snapshot


def apply_configuration_audit_filters(queryset, filters):
    """Apply the exact categorical and date filters shared by list and export."""
    for key in ("action", "target_type", "target_id"):
        if filters.get(key):
            queryset = queryset.filter(**{key: filters[key]})
    if filters.get("actor"):
        queryset = queryset.filter(actor_id=filters["actor"])
    if filters.get("created_after"):
        queryset = queryset.filter(created_at__gte=filters["created_after"])
    if filters.get("created_before"):
        queryset = queryset.filter(created_at__lte=filters["created_before"])
    return queryset


def scoped_configuration_audit_queryset(*, tenant=None, branch=None):
    queryset = ConfigurationAuditEvent.all_objects.select_related("actor")
    if branch is not None:
        return queryset.filter(branch=branch)
    if tenant is not None:
        return queryset.filter(tenant=tenant)
    return queryset.filter(tenant__isnull=True)


def _write_rows(writer, events):
    labels = build_configuration_target_labels(events)
    for event in events:
        target = labels.get((event.target_type, event.target_id)) or event.target_type
        writer.writerow([
            event.created_at.isoformat(),
            event.action,
            event.target_type,
            target,
            event.actor.full_name if event.actor else "System",
            event.actor.email if event.actor else "",
            event.reason,
            event.scope_key,
            json.dumps(event.before_data, ensure_ascii=True, sort_keys=True),
            json.dumps(event.after_data, ensure_ascii=True, sort_keys=True),
        ])


def execute_configuration_audit_export(job_id):
    """Stream a queued export to platform storage without holding it in memory."""
    job = ConfigurationAuditExportJob.objects.select_related(
        "requested_by", "tenant", "branch",
    ).filter(pk=job_id).first()
    if job is None:
        return None
    if job.status != job.Status.QUEUED:
        return job

    job.status = job.Status.RUNNING
    job.started_at = timezone.now()
    job.failure_message = ""
    job.save(update_fields=["status", "started_at", "failure_message"])
    storage_name = ""
    try:
        queryset = scoped_configuration_audit_queryset(
            tenant=job.tenant, branch=job.branch,
        ).order_by("-created_at")
        queryset = apply_configuration_audit_filters(queryset, job.filters)
        row_count = 0
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "created_at", "action", "target_type", "target", "actor_name",
                "actor_email", "reason", "scope", "before", "after",
            ])
            chunk = []
            for event in queryset.iterator(chunk_size=500):
                row_count += 1
                if row_count > EXPORT_ROW_LIMIT:
                    raise ValueError("ROW_LIMIT")
                chunk.append(event)
                if len(chunk) == 500:
                    _write_rows(writer, chunk)
                    chunk = []
            if chunk:
                _write_rows(writer, chunk)
            handle.flush()
            handle.seek(0)
            file_name = f"configuration-audit-{job.requested_at.date()}-{job.pk}.csv"
            storage_name = default_storage.save(
                f"configuration-audit/{job.pk}/{file_name}", File(handle, name=file_name),
            )

        completed_at = timezone.now()
        job.status = job.Status.COMPLETED
        job.file_name = file_name
        job.storage_name = storage_name
        job.row_count = row_count
        job.completed_at = completed_at
        job.available_until = completed_at + timedelta(days=EXPORT_RETENTION_DAYS)
        job.save(update_fields=[
            "status", "file_name", "storage_name", "row_count", "completed_at",
            "available_until",
        ])
        record_configuration_event(
            action="config.audit.export_completed",
            target=job,
            actor=job.requested_by,
            tenant=job.tenant,
            branch=job.branch,
            after={"row_count": row_count, "available_until": job.available_until.isoformat()},
            reason="Queued configuration audit export completed",
        )
    except Exception as exc:
        if storage_name:
            default_storage.delete(storage_name)
        logger.exception("Configuration audit export %s failed", job.pk)
        job.status = job.Status.FAILED
        job.completed_at = timezone.now()
        job.failure_message = (
            f"This export exceeds the {EXPORT_ROW_LIMIT:,}-row safety limit. "
            "Narrow the filters and try again."
            if str(exc) == "ROW_LIMIT"
            else "The export could not be generated. Try again or narrow the filters."
        )
        job.save(update_fields=["status", "completed_at", "failure_message"])
    return job


@transaction.atomic
def create_configuration_audit_export(*, actor, tenant, branch, filters, client_key=""):
    """Create one idempotent export job after enforcing per-actor queue limits."""
    # Serialize queue admission per user. Without this lock, simultaneous
    # requests could all observe fewer than three active jobs and overfill the
    # operator's queue before any transaction commits.
    type(actor).objects.select_for_update().only("pk").get(pk=actor.pk)
    now = timezone.now()
    if client_key:
        existing = ConfigurationAuditExportJob.objects.filter(
            requested_by=actor,
            client_key=client_key,
            requested_at__gte=now - timedelta(minutes=5),
        ).first()
        if existing is not None:
            return existing, False
    in_flight = ConfigurationAuditExportJob.objects.filter(
        requested_by=actor,
        status__in=[
            ConfigurationAuditExportJob.Status.QUEUED,
            ConfigurationAuditExportJob.Status.RUNNING,
        ],
    ).count()
    if in_flight >= CONCURRENT_EXPORT_LIMIT:
        raise ValueError("EXPORT_LIMIT")
    job = ConfigurationAuditExportJob.objects.create(
        requested_by=actor,
        tenant=tenant,
        branch=branch,
        filters=filters,
        client_key=client_key,
    )
    record_configuration_event(
        action="config.audit.export_queued",
        target=job,
        actor=actor,
        tenant=tenant,
        branch=branch,
        after={"status": job.status, "filters": filters},
        reason="Queued full configuration audit export",
    )
    return job, True
