from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from rest_framework import serializers as drf_serializers
from django.utils import timezone

from ..models import (
    ImportBatchStatusChoices,
    ImportJob,
    ImportJobRowResult,
    ImportJobStatusChoices,
    ImportRowActionChoices,
)
from .audit_service import create_import_audit_log

# Import your real app serializers here
# from apps.students.serializers import StudentCreateSerializer
# from apps.staff.serializers import StaffCreateSerializer


# =========================================================
# Result object returned by dataset handlers
# =========================================================
@dataclass
class ImportExecutionResult:
    action: str
    instance: Any | None
    target_model: str
    message: str = ""


# =========================================================
# Row -> payload mapping using official template columns
# =========================================================
def map_row_to_payload(import_batch, raw_row: dict) -> dict:
    """
    Convert uploaded row into internal payload using ImportTemplateColumn.
    """
    payload = {}

    template_columns = import_batch.template.columns.order_by("column_order")

    for template_column in template_columns:
        raw_value = raw_row.get(template_column.column_name)

        if raw_value in [None, ""] and template_column.default_value:
            raw_value = template_column.default_value

        payload[template_column.target_field] = raw_value

    return payload


# =========================================================
# Serializer helper
# =========================================================
def run_create_serializer(*, serializer_class, payload: dict, context: dict, target_model: str):
    """
    Reusable helper for executing a DRF create serializer inside import flow.
    """
    serializer = serializer_class(data=payload, context=context)
    serializer.is_valid(raise_exception=True)
    instance = serializer.save()

    return ImportExecutionResult(
        action=ImportRowActionChoices.CREATE,
        instance=instance,
        target_model=target_model,
        message=f"{target_model} created successfully.",
    )


# =========================================================
# Dataset-specific handler routing
# =========================================================
def execute_dataset_handler(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    dataset_type = import_batch.template.dataset_type

    if dataset_type == "students":
        return import_students_row(import_batch=import_batch, payload=payload, queued_by=queued_by)

    if dataset_type == "staff":
        return import_staff_row(import_batch=import_batch, payload=payload, queued_by=queued_by)

    if dataset_type == "classes":
        return import_classes_row(import_batch=import_batch, payload=payload, queued_by=queued_by)

    if dataset_type == "fees":
        return import_fees_row(import_batch=import_batch, payload=payload, queued_by=queued_by)

    raise ValueError(f"Unsupported dataset type: {dataset_type}")


# =========================================================
# Student import using create serializer
# =========================================================
def import_students_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """
    Import one student row using the Student create serializer.
    """

    serializer_payload = {
        **payload,
        "school": import_batch.school.pk,
    }

    context = {
        "request": None,
        "actor": queued_by,
        "school": import_batch.school,
        "import_batch": import_batch,
    }

    return run_create_serializer(
        # serializer_class=StudentCreateSerializer,
        payload=serializer_payload,
        context=context,
        target_model="Student",
    )


# =========================================================
# Staff import using create serializer
# =========================================================
def import_staff_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """
    Import one staff row using the Staff create serializer.
    """

    serializer_payload = {
        **payload,
        "school": import_batch.school.pk,
    }

    context = {
        "request": None,
        "actor": queued_by,
        "school": import_batch.school,
        "import_batch": import_batch,
    }

    return run_create_serializer(
        # serializer_class=StaffCreateSerializer,
        payload=serializer_payload,
        context=context,
        target_model="Staff",
    )


# =========================================================
# Placeholder handlers for datasets not yet connected
# =========================================================
def import_classes_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    raise NotImplementedError("Class import serializer is not connected yet.")


def import_fees_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    raise NotImplementedError("Fee import serializer is not connected yet.")


# =========================================================
# Job creation / setup
# =========================================================
@transaction.atomic
def start_import_job(import_batch, queued_by):
    total_rows = import_batch.total_rows or len(import_batch.preview_rows or [])

    job, _created = ImportJob.objects.get_or_create(
        import_batch=import_batch,
        defaults={
            "queued_by": queued_by,
            "status": ImportJobStatusChoices.QUEUED,
            "total_rows": total_rows,
        },
    )

    job.status = ImportJobStatusChoices.RUNNING
    job.started_at = timezone.now()
    job.completed_at = None
    job.progress_percent = 0
    job.processed_rows = 0
    job.succeeded_rows = 0
    job.failed_rows = 0
    job.skipped_rows = 0
    job.last_error_code = ""
    job.last_error_message = ""
    job.save(
        update_fields=[
            "status",
            "started_at",
            "completed_at",
            "progress_percent",
            "processed_rows",
            "succeeded_rows",
            "failed_rows",
            "skipped_rows",
            "last_error_code",
            "last_error_message",
            "updated_at",
        ]
    )

    import_batch.status = ImportBatchStatusChoices.IMPORT_RUNNING
    import_batch.save(update_fields=["status", "updated_at"])

    return job


# =========================================================
# Row result persistence
# =========================================================
def create_row_result(
    *,
    job,
    row_number: int,
    action: str,
    target_model: str,
    target_object_pk: str = "",
    status_message: str = "",
    error_details: dict | None = None,
    row_payload: dict | None = None,
    normalized_payload: dict | None = None,
):
    return ImportJobRowResult.objects.create(
        job=job,
        row_number=row_number,
        action=action,
        target_model=target_model,
        target_object_pk=target_object_pk,
        status_message=status_message,
        error_details=error_details or {},
        row_payload=row_payload or {},
        normalized_payload=normalized_payload or {},
    )


def update_job_progress(
    *,
    job,
    processed_rows: int,
    succeeded_rows: int,
    failed_rows: int,
    skipped_rows: int,
    total_rows: int,
):
    progress_percent = int((processed_rows / total_rows) * 100) if total_rows else 100

    job.processed_rows = processed_rows
    job.succeeded_rows = succeeded_rows
    job.failed_rows = failed_rows
    job.skipped_rows = skipped_rows
    job.progress_percent = progress_percent
    job.save(
        update_fields=[
            "processed_rows",
            "succeeded_rows",
            "failed_rows",
            "skipped_rows",
            "progress_percent",
            "updated_at",
        ]
    )


# =========================================================
# Main executor
# =========================================================
@transaction.atomic
def execute_import(import_batch, queued_by):
    if not import_batch.template:
        raise ValueError("This import batch has no selected template.")

    if not import_batch.is_ready_for_import:
        raise ValueError("This import batch is not ready for import.")

    rows = import_batch.preview_rows or []
    total_rows = len(rows)

    job = start_import_job(import_batch=import_batch, queued_by=queued_by)

    processed_rows = 0
    succeeded_rows = 0
    failed_rows = 0
    skipped_rows = 0

    for row_number, raw_row in enumerate(rows, start=1):
        try:
            normalized_payload = map_row_to_payload(import_batch, raw_row)

            result = execute_dataset_handler(
                import_batch=import_batch,
                payload=normalized_payload,
                queued_by=queued_by,
            )

            target_instance = result.instance
            target_object_pk = str(target_instance.pk) if target_instance else ""

            create_row_result(
                job=job,
                row_number=row_number,
                action=result.action,
                target_model=result.target_model,
                target_object_pk=target_object_pk,
                status_message=result.message,
                row_payload=raw_row,
                normalized_payload=normalized_payload,
            )

            create_import_audit_log(
                school=import_batch.school,
                actor=queued_by,
                import_batch=import_batch,
                job=job,
                action="import_row_success",
                entity_type=result.target_model,
                entity_id=target_object_pk,
                before_data={},
                after_data=normalized_payload,
                message=f"Imported row {row_number} successfully.",
                metadata={
                    "row_number": row_number,
                    "template_code": import_batch.template.code,
                    "template_version": import_batch.template_version,
                },
            )

            if result.action == ImportRowActionChoices.SKIP:
                skipped_rows += 1
            else:
                succeeded_rows += 1

        except drf_serializers.ValidationError as exc:
            create_row_result(
                job=job,
                row_number=row_number,
                action=ImportRowActionChoices.FAILED,
                target_model=import_batch.template.dataset_type,
                target_object_pk="",
                status_message="Serializer validation failed.",
                error_details={"validation_errors": exc.detail},
                row_payload=raw_row,
                normalized_payload=normalized_payload if "normalized_payload" in locals() else {},
            )

            failed_rows += 1

        except Exception as exc:
            create_row_result(
                job=job,
                row_number=row_number,
                action=ImportRowActionChoices.FAILED,
                target_model=import_batch.template.dataset_type,
                target_object_pk="",
                status_message="Import failed.",
                error_details={"error": str(exc)},
                row_payload=raw_row,
                normalized_payload=normalized_payload if "normalized_payload" in locals() else {},
            )

            failed_rows += 1

        processed_rows += 1

        update_job_progress(
            job=job,
            processed_rows=processed_rows,
            succeeded_rows=succeeded_rows,
            failed_rows=failed_rows,
            skipped_rows=skipped_rows,
            total_rows=total_rows,
        )

    finalize_import_job(
        import_batch=import_batch,
        job=job,
        processed_rows=processed_rows,
        succeeded_rows=succeeded_rows,
        failed_rows=failed_rows,
        skipped_rows=skipped_rows,
    )

    return job


# =========================================================
# Finalization
# =========================================================
def finalize_import_job(
    *,
    import_batch,
    job,
    processed_rows: int,
    succeeded_rows: int,
    failed_rows: int,
    skipped_rows: int,
):
    job.completed_at = timezone.now()
    job.execution_summary = {
        "processed_rows": processed_rows,
        "succeeded_rows": succeeded_rows,
        "failed_rows": failed_rows,
        "skipped_rows": skipped_rows,
        "template_code": import_batch.template.code if import_batch.template else "",
        "template_version": import_batch.template_version,
    }

    if failed_rows > 0 and succeeded_rows > 0:
        job.status = ImportJobStatusChoices.SUCCEEDED
        import_batch.status = ImportBatchStatusChoices.IMPORT_PARTIAL

    elif failed_rows > 0 and succeeded_rows == 0:
        job.status = ImportJobStatusChoices.FAILED
        import_batch.status = ImportBatchStatusChoices.IMPORT_FAILED

    else:
        job.status = ImportJobStatusChoices.SUCCEEDED
        import_batch.status = ImportBatchStatusChoices.IMPORT_SUCCEEDED
        import_batch.imported_at = timezone.now()

    job.save(
        update_fields=[
            "completed_at",
            "execution_summary",
            "status",
            "updated_at",
        ]
    )

    import_batch.save(
        update_fields=[
            "status",
            "imported_at",
            "updated_at",
        ]
    )