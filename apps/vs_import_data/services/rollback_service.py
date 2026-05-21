from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import (
    ImportBatchStatusChoices,
    ImportJobStatusChoices,
    ImportRollbackRecord,
)
from .audit_service import create_import_audit_log


def reverse_target_record(row_result):
    """
    Reverse one imported row. For schools: delete the School record that was created.
    The cascade takes care of branches, admins, and package setup.
    """
    from vs_schools.models import School

    pk = row_result.target_object_pk
    if not pk:
        return True

    try:
        School.objects.filter(slug=pk).delete()
        return True
    except Exception:
        return False


@transaction.atomic
def rollback_import_job(job, initiated_by=None, reason: str = ""):
    """
    Roll back imported rows for a job.
    """
    job.rollback_started_at = timezone.now()
    job.save(update_fields=["rollback_started_at", "updated_at"])

    rollback_record = ImportRollbackRecord.objects.create(
        job=job,
        initiated_by=initiated_by,
        reason=reason,
        started_at=timezone.now(),
    )

    reverted_rows_count = 0

    for row_result in job.row_results.exclude(target_object_pk=""):
        success = reverse_target_record(row_result)
        if success:
            reverted_rows_count += 1

    rollback_record.was_successful = True
    rollback_record.reverted_rows_count = reverted_rows_count
    rollback_record.completed_at = timezone.now()
    rollback_record.details = {
        "reverted_rows_count": reverted_rows_count,
    }
    rollback_record.save(
        update_fields=[
            "was_successful",
            "reverted_rows_count",
            "completed_at",
            "details",
            "updated_at",
        ]
    )

    job.status = ImportJobStatusChoices.ROLLED_BACK
    job.rollback_completed_at = timezone.now()
    job.save(update_fields=["status", "rollback_completed_at", "updated_at"])

    import_batch = job.import_batch
    import_batch.status = ImportBatchStatusChoices.ROLLED_BACK
    import_batch.save(update_fields=["status", "updated_at"])

    create_import_audit_log(
        school=import_batch.school,
        branch=import_batch.branch,
        actor=initiated_by,
        import_batch=import_batch,
        job=job,
        action="import_rollback",
        entity_type="import_job",
        entity_id=str(job.id),
        before_data={"status": "imported"},
        after_data={"status": "rolled_back"},
        message="Import job rolled back successfully.",
        metadata={"reason": reason},
    )

    return rollback_record