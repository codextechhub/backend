from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import (
    ImportBatchStatusChoices,
    ImportJobStatusChoices,
    ImportRollbackRecord,
)
from .audit_service import create_import_audit_log
from .reversers import FAILED, REFUSED, REVERTED, SKIPPED, reverse_row


def _rows_already_reverted(job) -> set[int]:
    """Row numbers an earlier rollback of this job already reversed.

    A partial rollback is retryable: the operator deletes whatever was blocking
    a row and runs it again. Without this, the second run would report every
    row it reversed the first time as "no such record exists" - true, alarming,
    and useless.
    """
    reverted: set[int] = set()

    for record in job.rollback_records.all():
        details = record.details if isinstance(record.details, dict) else {}
        for row in details.get("rows") or []:
            if isinstance(row, dict) and row.get("status") == REVERTED:
                row_number = row.get("row_number")
                if isinstance(row_number, int):
                    reverted.add(row_number)

    return reverted


@transaction.atomic
def rollback_import_job(job, initiated_by=None, reason: str = ""):
    """
    Roll back imported rows for a job, and report honestly what happened.

    Each row is reversed by the model it actually created (see ``reversers``),
    in its own savepoint. A row that cannot be reversed - because no rollback is
    defined for its model, because the record it names is not the one it
    created, or because the school it created has been used since - leaves
    everything alone and is counted separately.

    The job is only marked ROLLED_BACK when every row was in fact reversed.
    Anything less is PARTIALLY_ROLLED_BACK, which is a state an operator can see
    and act on, and which the rollback endpoint will accept again once whatever
    blocked a row has been dealt with.
    """
    if (
        job.import_batch.template
        and job.import_batch.template.dataset_type == "bank_statements"
    ):
        from vs_finance.statement_imports import rollback_bank_statement_import_job

        return rollback_bank_statement_import_job(
            job,
            initiated_by=initiated_by,
            reason=reason,
        )

    already_reverted = _rows_already_reverted(job)

    job.rollback_started_at = timezone.now()
    job.save(update_fields=["rollback_started_at", "updated_at"])

    rollback_record = ImportRollbackRecord.objects.create(
        job=job,
        initiated_by=initiated_by,
        reason=reason,
        started_at=timezone.now(),
    )

    outcomes = []

    for row_result in job.row_results.exclude(target_object_pk="").select_related(
        "job__import_batch"
    ):
        if row_result.row_number in already_reverted:
            outcomes.append(
                {
                    "row_number": row_result.row_number,
                    "target_model": row_result.target_model,
                    "target_object_pk": row_result.target_object_pk,
                    "status": SKIPPED,
                    "message": "Already reversed by an earlier rollback.",
                }
            )
            continue

        outcomes.append(
            reverse_row(row_result, initiated_by=initiated_by).as_dict()
        )

    counts = {
        status: sum(1 for outcome in outcomes if outcome["status"] == status)
        for status in (REVERTED, REFUSED, FAILED, SKIPPED)
    }
    fully_reverted = not counts[REFUSED] and not counts[FAILED]

    rollback_record.was_successful = fully_reverted
    rollback_record.reverted_rows_count = counts[REVERTED]
    rollback_record.completed_at = timezone.now()
    rollback_record.details = {
        "reverted_rows_count": counts[REVERTED],
        "refused_rows_count": counts[REFUSED],
        "failed_rows_count": counts[FAILED],
        "skipped_rows_count": counts[SKIPPED],
        "rows": outcomes,
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

    job.status = (
        ImportJobStatusChoices.ROLLED_BACK
        if fully_reverted
        else ImportJobStatusChoices.PARTIALLY_ROLLED_BACK
    )
    job.rollback_completed_at = timezone.now()
    job.save(update_fields=["status", "rollback_completed_at", "updated_at"])

    import_batch = job.import_batch
    import_batch.status = (
        ImportBatchStatusChoices.ROLLED_BACK
        if fully_reverted
        else ImportBatchStatusChoices.PARTIALLY_ROLLED_BACK
    )
    import_batch.save(update_fields=["status", "updated_at"])

    unreversed = counts[REFUSED] + counts[FAILED]
    message = (
        "Import job rolled back successfully."
        if fully_reverted
        else (
            f"Import job partially rolled back: {counts[REVERTED]} of "
            f"{len(outcomes)} rows reversed, {unreversed} could not be."
        )
    )

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
        after_data={"status": job.status},
        message=message,
        metadata={"reason": reason, "outcomes": counts},
    )

    return rollback_record
