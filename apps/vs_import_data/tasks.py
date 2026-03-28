from __future__ import annotations

import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import (
    ImportBatch,
    ImportBatchStatusChoices,
    ImportJob,
    ImportJobStatusChoices,
    ImportNotification,
    NotificationStatusChoices,
)
from .services.audit_service import create_import_audit_log
from .services.import_executor import execute_import
from .services.rollback_service import rollback_import_job
from .services.validation_service import validate_import_batch

logger = logging.getLogger(__name__)
User = get_user_model()


# =========================================================
# Small helpers
# =========================================================
def _safe_error_text(exc: Exception) -> str:
    """
    Convert exception to a safe string for logs/database storage.
    """
    return str(exc).strip() or exc.__class__.__name__


def _get_user_or_none(user_id: str | None):
    """
    Safely fetch a user if an id was supplied.
    """
    if not user_id:
        return None

    try:
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        return None


# =========================================================
# 1. Validate import batch in background
# =========================================================
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def validate_import_batch_task(self, import_batch_id: str) -> dict:
    """
    Run validation for a template-driven import batch in the background.
    """
    import_batch = ImportBatch.objects.select_related(
        "institution",
        "uploaded_by",
        "template",
    ).prefetch_related(
        "template__columns",
    ).get(id=import_batch_id)

    try:
        result = validate_import_batch(import_batch)

        create_import_audit_log(
            institution=import_batch.institution,
            actor=import_batch.uploaded_by,
            import_batch=import_batch,
            action="validation_completed",
            entity_type="import_batch",
            entity_id=str(import_batch.id),
            before_data={},
            after_data={
                "status": import_batch.status,
                "has_critical_errors": import_batch.has_critical_errors,
                "is_ready_for_import": import_batch.is_ready_for_import,
                "validation_summary": import_batch.validation_summary,
                "template_code": getattr(import_batch.template, "code", ""),
                "template_version": import_batch.template_version,
            },
            message="Background validation completed.",
            metadata={"summary": result["summary"]},
        )

        ImportNotification.objects.create(
            import_batch=import_batch,
            recipient=import_batch.uploaded_by,
            event_type="validation_completed",
            title="Import validation completed",
            body=f"Validation finished for file '{import_batch.original_filename}'.",
        )

        return {
            "import_batch_id": str(import_batch.id),
            "status": import_batch.status,
            "summary": result["summary"],
        }

    except Exception as exc:
        error_text = _safe_error_text(exc)

        import_batch.status = ImportBatchStatusChoices.VALIDATION_FAILED
        import_batch.validation_completed_at = timezone.now()
        import_batch.notes = f"{import_batch.notes}\nValidation task failed: {error_text}".strip()
        import_batch.save(
            update_fields=[
                "status",
                "validation_completed_at",
                "notes",
                "updated_at",
            ]
        )

        logger.exception("Validation task failed for import batch %s", import_batch_id)

        create_import_audit_log(
            institution=import_batch.institution,
            actor=import_batch.uploaded_by,
            import_batch=import_batch,
            action="validation_failed",
            entity_type="import_batch",
            entity_id=str(import_batch.id),
            before_data={},
            after_data={
                "status": import_batch.status,
                "template_code": getattr(import_batch.template, "code", ""),
                "template_version": import_batch.template_version,
            },
            message="Background validation failed.",
            metadata={"error": error_text},
        )
        raise


# =========================================================
# 2. Execute import batch in background
# =========================================================
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def execute_import_batch_task(self, import_batch_id: str, queued_by_id: str | None = None) -> dict:
    """
    Execute the actual import in the background.
    Uses the template-driven, serializer-based import executor.
    """
    import_batch = ImportBatch.objects.select_related(
        "institution",
        "uploaded_by",
        "template",
    ).prefetch_related(
        "template__columns",
    ).get(id=import_batch_id)

    if not import_batch.template:
        raise ValueError("Import batch has no selected template.")

    if not import_batch.is_ready_for_import:
        raise ValueError("Import batch is not ready for import.")

    queued_by = _get_user_or_none(queued_by_id) or import_batch.uploaded_by

    try:
        job = execute_import(import_batch=import_batch, queued_by=queued_by)

        # Save Celery task id for traceability
        if getattr(self.request, "id", None):
            job.task_id = self.request.id
            job.save(update_fields=["task_id", "updated_at"])

        ImportNotification.objects.create(
            import_batch=import_batch,
            recipient=queued_by,
            event_type="import_completed",
            title="Import job completed",
            body=(
                f"Import finished for file '{import_batch.original_filename}' "
                f"with status '{job.status}'."
            ),
        )

        create_import_audit_log(
            institution=import_batch.institution,
            actor=queued_by,
            import_batch=import_batch,
            job=job,
            action="import_task_completed",
            entity_type="import_job",
            entity_id=str(job.id),
            before_data={},
            after_data={
                "job_status": job.status,
                "batch_status": import_batch.status,
                "execution_summary": job.execution_summary,
                "template_code": getattr(import_batch.template, "code", ""),
                "template_version": import_batch.template_version,
            },
            message="Background import execution completed.",
            metadata={"task_id": job.task_id},
        )

        return {
            "import_batch_id": str(import_batch.id),
            "job_id": str(job.id),
            "job_status": job.status,
            "batch_status": import_batch.status,
            "execution_summary": job.execution_summary,
        }

    except Exception as exc:
        error_text = _safe_error_text(exc)

        job = getattr(import_batch, "import_job", None)
        if job:
            job.status = ImportJobStatusChoices.FAILED
            job.completed_at = timezone.now()
            job.last_error_message = error_text
            if getattr(self.request, "id", None):
                job.task_id = self.request.id
            job.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "last_error_message",
                    "task_id",
                    "updated_at",
                ]
            )

        import_batch.status = ImportBatchStatusChoices.IMPORT_FAILED
        import_batch.notes = f"{import_batch.notes}\nImport task failed: {error_text}".strip()
        import_batch.save(update_fields=["status", "notes", "updated_at"])

        logger.exception("Import execution task failed for import batch %s", import_batch_id)

        create_import_audit_log(
            institution=import_batch.institution,
            actor=queued_by,
            import_batch=import_batch,
            job=job,
            action="import_task_failed",
            entity_type="import_batch",
            entity_id=str(import_batch.id),
            before_data={},
            after_data={
                "status": import_batch.status,
                "template_code": getattr(import_batch.template, "code", ""),
                "template_version": import_batch.template_version,
            },
            message="Background import execution failed.",
            metadata={"error": error_text},
        )
        raise


# =========================================================
# 3. Rollback import job in background
# =========================================================
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def rollback_import_job_task(
    self,
    job_id: str,
    initiated_by_id: str | None = None,
    reason: str = "",
) -> dict:
    """
    Run rollback in the background.
    """
    job = ImportJob.objects.select_related(
        "import_batch",
        "import_batch__institution",
        "import_batch__template",
        "queued_by",
    ).get(id=job_id)

    initiated_by = _get_user_or_none(initiated_by_id)

    try:
        rollback_record = rollback_import_job(
            job=job,
            initiated_by=initiated_by,
            reason=reason,
        )

        recipient = initiated_by or job.queued_by

        if recipient:
            ImportNotification.objects.create(
                import_batch=job.import_batch,
                recipient=recipient,
                event_type="import_rollback_completed",
                title="Import rollback completed",
                body=f"Rollback finished for import job '{job.id}'.",
            )

        create_import_audit_log(
            institution=job.import_batch.institution,
            actor=initiated_by,
            import_batch=job.import_batch,
            job=job,
            action="rollback_completed",
            entity_type="import_job",
            entity_id=str(job.id),
            before_data={},
            after_data={
                "job_status": job.status,
                "batch_status": job.import_batch.status,
                "template_code": getattr(job.import_batch.template, "code", ""),
                "template_version": job.import_batch.template_version,
            },
            message="Background rollback completed.",
            metadata={"reason": reason},
        )

        return {
            "job_id": str(job.id),
            "rollback_id": str(rollback_record.id),
            "was_successful": rollback_record.was_successful,
            "reverted_rows_count": rollback_record.reverted_rows_count,
        }

    except Exception as exc:
        error_text = _safe_error_text(exc)
        logger.exception("Rollback task failed for job %s", job_id)

        create_import_audit_log(
            institution=job.import_batch.institution,
            actor=initiated_by,
            import_batch=job.import_batch,
            job=job,
            action="rollback_failed",
            entity_type="import_job",
            entity_id=str(job.id),
            before_data={},
            after_data={},
            message="Background rollback failed.",
            metadata={"error": error_text},
        )
        raise


# =========================================================
# 4. Send one notification
# =========================================================
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def send_import_notification_task(self, notification_id: str) -> dict:
    """
    Send one notification.

    For now, this only marks the notification as sent.
    Later you can connect:
    - email
    - websocket
    - in-app push
    """
    notification = ImportNotification.objects.select_related(
        "import_batch",
        "recipient",
    ).get(id=notification_id)

    try:
        notification.status = NotificationStatusChoices.SENT
        notification.sent_at = timezone.now()
        notification.error_message = ""
        notification.save(update_fields=["status", "sent_at", "error_message", "updated_at"])

        return {
            "notification_id": str(notification.id),
            "status": notification.status,
        }

    except Exception as exc:
        error_text = _safe_error_text(exc)

        notification.status = NotificationStatusChoices.FAILED
        notification.error_message = error_text
        notification.save(update_fields=["status", "error_message", "updated_at"])

        logger.exception("Notification send failed for notification %s", notification_id)
        raise


# =========================================================
# 5. Retry failed notifications
# =========================================================
@shared_task
def retry_failed_import_notifications_task() -> dict:
    """
    Retry failed notification records.
    Useful as a periodic Celery beat task.
    """
    failed_notifications = ImportNotification.objects.filter(
        status=NotificationStatusChoices.FAILED
    ).values_list("id", flat=True)

    count = 0
    for notification_id in failed_notifications:
        send_import_notification_task.delay(str(notification_id))
        count += 1

    return {"queued_notifications": count}


# =========================================================
# 6. Cleanup stale import batches
# =========================================================
@shared_task
def cleanup_old_import_batches_task(days: int = 30) -> dict:
    """
    Mark stale unfinished batches as cancelled.
    Good for periodic maintenance.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=days)

    stale_batches = ImportBatch.objects.filter(
        created_at__lt=cutoff,
        status__in=[
            ImportBatchStatusChoices.DRAFT,
            ImportBatchStatusChoices.UPLOADED,
            ImportBatchStatusChoices.VALIDATING,
        ],
    )

    updated_count = stale_batches.update(
        status=ImportBatchStatusChoices.CANCELLED,
        updated_at=timezone.now(),
    )

    return {
        "cutoff": cutoff.isoformat(),
        "cancelled_batches": updated_count,
    }


# =========================================================
# 7. Mark stuck running jobs as failed
# =========================================================
@shared_task
def mark_stuck_import_jobs_task(minutes: int = 120) -> dict:
    """
    Mark long-running jobs as failed if they exceed allowed runtime.
    Useful as a safety-net periodic task.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=minutes)

    stuck_jobs = ImportJob.objects.filter(
        status=ImportJobStatusChoices.RUNNING,
        started_at__lt=cutoff,
    ).select_related(
        "import_batch",
        "import_batch__institution",
        "import_batch__template",
    )

    count = 0

    for job in stuck_jobs:
        with transaction.atomic():
            job.status = ImportJobStatusChoices.FAILED
            job.completed_at = timezone.now()
            job.last_error_message = (
                "Job marked as failed automatically because it exceeded allowed runtime."
            )
            job.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "last_error_message",
                    "updated_at",
                ]
            )

            job.import_batch.status = ImportBatchStatusChoices.IMPORT_FAILED
            job.import_batch.save(update_fields=["status", "updated_at"])

            create_import_audit_log(
                institution=job.import_batch.institution,
                actor=None,
                import_batch=job.import_batch,
                job=job,
                action="job_marked_stuck_failed",
                entity_type="import_job",
                entity_id=str(job.id),
                before_data={"status": "running"},
                after_data={
                    "status": "failed",
                    "template_code": getattr(job.import_batch.template, "code", ""),
                    "template_version": job.import_batch.template_version,
                },
                message="Job automatically marked as failed because it was stuck.",
                metadata={"runtime_limit_minutes": minutes},
            )

            count += 1

    return {"marked_failed_jobs": count}


# =========================================================
# 8. Queue notifications for pending records
# =========================================================
@shared_task
def dispatch_pending_import_notifications_task() -> dict:
    """
    Find pending notifications and queue them for sending.
    Useful if you want a scheduled dispatcher.
    """
    pending_notifications = ImportNotification.objects.filter(
        status=NotificationStatusChoices.PENDING
    ).values_list("id", flat=True)

    count = 0
    for notification_id in pending_notifications:
        send_import_notification_task.delay(str(notification_id))
        count += 1

    return {"queued_notifications": count}