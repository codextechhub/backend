"""Background tasks owned by Platform Settings."""

from celery import shared_task


@shared_task(name="vs_config.run_configuration_audit_export")
def run_configuration_audit_export_task(job_id):
    from .services.audit_exports import execute_configuration_audit_export

    job = execute_configuration_audit_export(job_id)
    if job is None:
        return {"job": str(job_id), "status": "MISSING"}
    return {"job": str(job.pk), "status": job.status, "rows": job.row_count}
