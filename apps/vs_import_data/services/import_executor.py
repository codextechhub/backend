from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import (
    ImportBatchStatusChoices,
    ImportJob,
    ImportJobRowResult,
    ImportJobStatusChoices,
    ImportRowActionChoices,
)
from .audit_service import create_import_audit_log


def map_row_to_payload(import_batch, raw_row: dict) -> dict:
    """
    Convert uploaded row keys into target field keys using saved mappings.
    """
    payload = {}

    mappings = import_batch.column_mappings.all()
    for mapping in mappings:
        payload[mapping.target_field] = raw_row.get(mapping.source_column)

    return payload


def create_or_update_target_record(import_batch, payload: dict):
    """
    Replace this with your real import logic.

    Example:
    - if dataset_type == 'students': create Student
    - if dataset_type == 'staff': create Staff
    """
    # -------------------------
    # DEMO IMPLEMENTATION ONLY
    # -------------------------
    class DummyObject:
        pk = "demo-pk-123"

    return {
        "action": ImportRowActionChoices.CREATE,
        "instance": DummyObject(),
        "target_model": import_batch.dataset_type,
    }


@transaction.atomic
def start_import_job(import_batch, queued_by):
    """
    Create or reuse the job record for an import batch.
    """
    job, _created = ImportJob.objects.get_or_create(
        import_batch=import_batch,
        defaults={
            "queued_by": queued_by,
            "status": ImportJobStatusChoices.QUEUED,
            "total_rows": import_batch.total_rows or len(import_batch.preview_rows or []),
        },
    )

    job.status = ImportJobStatusChoices.RUNNING
    job.started_at = timezone.now()
    job.progress_percent = 0
    job.save(update_fields=["status", "started_at", "progress_percent", "updated_at"])

    import_batch.status = ImportBatchStatusChoices.IMPORT_RUNNING
    import_batch.save(update_fields=["status", "updated_at"])

    return job


@transaction.atomic
def execute_import(import_batch, queued_by):
    """
    Main import runner.
    """
    if not import_batch.is_ready_for_import:
        raise ValueError("This import batch is not ready for import.")

    job = start_import_job(import_batch=import_batch, queued_by=queued_by)
    rows = import_batch.preview_rows or []

    processed_rows = 0
    succeeded_rows = 0
    failed_rows = 0
    skipped_rows = 0

    for row_number, raw_row in enumerate(rows, start=1):
        try:
            normalized_payload = map_row_to_payload(import_batch, raw_row)
            result = create_or_update_target_record(import_batch, normalized_payload)

            target_instance = result["instance"]
            action = result["action"]
            target_model = result["target_model"]

            ImportJobRowResult.objects.create(
                job=job,
                row_number=row_number,
                action=action,
                target_model=target_model,
                target_object_pk=str(target_instance.pk),
                status_message="Imported successfully.",
                row_payload=raw_row,
                normalized_payload=normalized_payload,
            )

            create_import_audit_log(
                branch=import_batch.branch,
                actor=queued_by,
                import_batch=import_batch,
                job=job,
                action="import_row_success",
                entity_type=target_model,
                entity_id=str(target_instance.pk),
                before_data={},
                after_data=normalized_payload,
                message=f"Imported row {row_number} successfully.",
                metadata={"row_number": row_number},
            )

            succeeded_rows += 1

        except Exception as exc:
            ImportJobRowResult.objects.create(
                job=job,
                row_number=row_number,
                action=ImportRowActionChoices.FAILED,
                target_model=import_batch.dataset_type,
                target_object_pk="",
                status_message="Import failed.",
                error_details={"error": str(exc)},
                row_payload=raw_row,
                normalized_payload={},
            )
            failed_rows += 1

        processed_rows += 1
        progress = int((processed_rows / len(rows)) * 100) if rows else 100

        job.processed_rows = processed_rows
        job.succeeded_rows = succeeded_rows
        job.failed_rows = failed_rows
        job.skipped_rows = skipped_rows
        job.progress_percent = progress
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

    job.completed_at = timezone.now()
    job.execution_summary = {
        "processed_rows": processed_rows,
        "succeeded_rows": succeeded_rows,
        "failed_rows": failed_rows,
        "skipped_rows": skipped_rows,
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

    job.save(update_fields=["completed_at", "execution_summary", "status", "updated_at"])
    import_batch.save(update_fields=["status", "imported_at", "updated_at"])

    return job