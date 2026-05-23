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
    if not import_batch.template:
        raise ValueError("ImportBatch has no template selected.")

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

    if dataset_type == "schools":
        return import_schools_row(import_batch=import_batch, payload=payload, queued_by=queued_by)

    if dataset_type == "branches":
        return import_branches_row(import_batch=import_batch, payload=payload, queued_by=queued_by)

    raise ValueError(f"Unsupported dataset type: {dataset_type}")


# =========================================================
# Schools handler
# =========================================================
def import_schools_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """
    Import one school row using SchoolCreateSerializer.

    The flat ImportTemplateColumn.target_field names this handler reads:

    School identity
        name                    required
        slug                    optional – auto-generated from name if blank
        code                    optional
        ownership_type          optional – PUBLIC / PRIVATE / FAITH_BASED / NGO
        address                 optional
        website                 optional
        motto                   optional
        term_structure          optional – 3_TERMS / 2_SEMESTERS
        currency                optional – NGN / USD
        registration_id         optional

    School-level admin
        school_admin_full_name  required when school_admin_email is present
        school_admin_email      optional – if absent no school admin is created
        school_admin_phone      optional
        school_admin_role       optional – defaults to "IT Head"

    Main branch  (one branch per row, always marked is_main=True)
        branch_name             optional – defaults to "<school name> — Main Campus"
        branch_type             optional – defaults to "Combined"
        branch_address          optional – falls back to school address
        branch_email            optional
        branch_country          optional – defaults to "Nigeria"
        branch_state            optional

    Branch admin  (required – SchoolCreateSerializer enforces this)
        branch_admin_full_name  required
        branch_admin_email      required
        branch_admin_phone      optional
        branch_admin_role       optional – defaults to "Head Teacher"

    Package setup
        package_plan            optional – PackagePlan code e.g. basic / standard / premium
        student_capacity        optional – defaults to 50
        teacher_capacity        optional – defaults to 10
        admin_capacity          optional – defaults to 3
        enabled_modules         optional – comma-separated module keys e.g. "students,attendance"
        subscription_expires_at optional – YYYY-MM-DD
    """
    from types import SimpleNamespace
    from vs_schools.serializers import SchoolCreateSerializer

    def _s(key: str) -> str:
        return (payload.get(key) or "").strip()

    def _int(key: str, default: int) -> int:
        try:
            return int(payload.get(key) or default)
        except (TypeError, ValueError):
            return default

    # --- School-level admin ---
    school_admin_email = _s("school_admin_email")
    primary_admin_data = None
    if school_admin_email:
        primary_admin_data = {
            "full_name": _s("school_admin_full_name"),
            "email": school_admin_email,
            "phone": _s("school_admin_phone"),
            "school_role": _s("school_admin_role") or "IT Head",
            "role_label": "SCHOOL_ADMIN",
        }

    # --- Branch admin ---
    branch_admin_data = {
        "full_name": _s("branch_admin_full_name"),
        "email": _s("branch_admin_email"),
        "phone": _s("branch_admin_phone"),
        "branch_role": _s("branch_admin_role") or "Head Teacher",
        "role_label": "BRANCH_ADMIN",
    }

    # --- Branch ---
    branch_name = _s("branch_name") or f"{_s('name')} — Main Campus"
    branch = {
        "name": branch_name,
        "_type": _s("branch_type") or "Combined",
        "address": _s("branch_address") or _s("address"),
        "email": _s("branch_email"),
        "country": _s("branch_country") or "Nigeria",
        "state": _s("branch_state"),
        "is_main": True,
        "primary_admin_data": branch_admin_data,
    }

    # --- Package setup ---
    package_plan_code = _s("package_plan")
    package_setup_data = None
    if package_plan_code:
        raw_modules = _s("enabled_modules")
        enabled_modules = [m.strip() for m in raw_modules.split(",") if m.strip()] if raw_modules else []
        package_setup_data = {
            "package_plan": package_plan_code,
            "enabled_modules": enabled_modules,
            "student_capacity": _int("student_capacity", 50),
            "teacher_capacity": _int("teacher_capacity", 10),
            "admin_capacity": _int("admin_capacity", 3),
        }
        sub_expires = _s("subscription_expires_at")
        if sub_expires:
            package_setup_data["subscription_expires_at"] = sub_expires

    # --- Assemble school payload ---
    school_payload: dict = {
        "name": _s("name"),
        "branches": [branch],
    }

    for field in ("slug", "code", "address", "website", "motto", "registration_id"):
        val = _s(field)
        if val:
            school_payload[field] = val

    # Choice fields: only include if non-empty so model defaults apply when blank
    for field in ("ownership_type", "term_structure", "currency"):
        val = _s(field)
        if val:
            school_payload[field] = val

    if primary_admin_data:
        school_payload["primary_admin_data"] = primary_admin_data

    if package_setup_data:
        school_payload["package_setup_data"] = package_setup_data

    # SchoolCreateSerializer accesses context["request"].user as the acting user.
    # SimpleNamespace gives us a minimal stand-in without importing django.test.
    context = {
        "request": SimpleNamespace(user=queued_by),
        "actor_id": str(queued_by.id),
    }

    return run_create_serializer(
        serializer_class=SchoolCreateSerializer,
        payload=school_payload,
        context=context,
        target_model="School",
    )


# =========================================================
# Branches handler
# =========================================================
def import_branches_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """
    Import one branch row using BranchCreateSerializer.

    School resolution (in priority order):
      1. batch is school-scoped  → use import_batch.school
      2. row contains school_slug → look up School by slug
      3. row contains school_code → look up School by code
    If none resolve, the row fails.

    Template columns this handler reads:

    School identifier (only needed when batch is not school-scoped)
        school_slug             conditional
        school_code             conditional

    Branch identity
        name                    required
        branch_type             optional – defaults to "Combined"
        address                 optional
        email                   optional
        country                 optional – defaults to "Nigeria"
        state                   optional
        is_main                 optional – "true"/"false", defaults to False

    Branch admin (required by BranchCreateSerializer)
        branch_admin_full_name  required
        branch_admin_email      required
        branch_admin_phone      optional
        branch_admin_role       optional – defaults to "Head Teacher"
    """
    from types import SimpleNamespace
    from vs_schools.models import School
    from vs_schools.serializers import BranchCreateSerializer

    def _s(key: str) -> str:
        return (payload.get(key) or "").strip()

    # --- Resolve school ---
    school = import_batch.school
    if school is None:
        slug = _s("school_slug")
        code = _s("school_code")
        if slug:
            try:
                school = School.objects.get(slug=slug)
            except School.DoesNotExist:
                raise ValueError(f"No school found with slug '{slug}'.")
        elif code:
            try:
                school = School.objects.get(code=code)
            except School.DoesNotExist:
                raise ValueError(f"No school found with code '{code}'.")
        else:
            raise ValueError(
                "Cannot determine school: batch is not school-scoped and row has no school_slug or school_code."
            )

    # --- Branch admin ---
    branch_admin_data = {
        "full_name": _s("branch_admin_full_name"),
        "email": _s("branch_admin_email"),
        "phone": _s("branch_admin_phone"),
        "branch_role": _s("branch_admin_role") or "Head Teacher",
        "role_label": "BRANCH_ADMIN",
    }

    # --- Branch payload ---
    is_main_raw = _s("is_main").lower()
    branch_payload = {
        "name": _s("name"),
        "_type": _s("branch_type") or "Combined",
        "is_main": is_main_raw in ("true", "1", "yes"),
        "primary_admin_data": branch_admin_data,
    }

    for field in ("address", "email", "state"):
        val = _s(field)
        if val:
            branch_payload[field] = val

    branch_payload["country"] = _s("country") or "Nigeria"

    context = {
        "request": SimpleNamespace(user=queued_by),
        "school": school,
        "actor_id": str(queued_by.id),
    }

    return run_create_serializer(
        serializer_class=BranchCreateSerializer,
        payload=branch_payload,
        context=context,
        target_model="Branch",
    )


# =========================================================
# Job creation / setup
# =========================================================
@transaction.atomic
def start_import_job(import_batch, queued_by):
    total_rows = import_batch.total_rows or len(import_batch.preview_rows or [])

    job, _ = ImportJob.objects.get_or_create(
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
def execute_import(import_batch, queued_by):
    if not import_batch.template:
        raise ValueError("This import batch has no selected template.")

    if not import_batch.is_ready_for_import:
        raise ValueError("This import batch is not ready for import.")

    rows = import_batch.preview_rows or []
    total_rows = len(rows)

    # start_import_job is its own @transaction.atomic — committed immediately.
    job = start_import_job(import_batch=import_batch, queued_by=queued_by)

    processed_rows = 0
    succeeded_rows = 0
    failed_rows = 0
    skipped_rows = 0

    for row_number, raw_row in enumerate(rows, start=1):
        # Each row is committed in its own savepoint so a mid-run crash does
        # not roll back results that were already saved for earlier rows.
        normalized_payload = {}
        try:
            with transaction.atomic():
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
                    branch=import_batch.branch,
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
                        "template_version": import_batch.template.version if import_batch.template else "",
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
                normalized_payload=normalized_payload,
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
                normalized_payload=normalized_payload,
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
        "template_version": import_batch.template.version if import_batch.template else "",
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

    audit_action = "import_failed" if (failed_rows > 0 and succeeded_rows == 0) else "import_completed"
    create_import_audit_log(
        school=import_batch.school,
        branch=import_batch.branch,
        actor=job.queued_by,
        import_batch=import_batch,
        job=job,
        action=audit_action,
        entity_type="ImportJob",
        entity_id=str(job.id),
        before_data={"status": ImportJobStatusChoices.RUNNING},
        after_data={"status": job.status},
        message=(
            f"Import job completed. {succeeded_rows}/{processed_rows} rows succeeded."
            if audit_action == "import_completed"
            else f"Import job failed. {failed_rows}/{processed_rows} rows failed."
        ),
        metadata={
            "processed_rows": processed_rows,
            "succeeded_rows": succeeded_rows,
            "failed_rows": failed_rows,
            "skipped_rows": skipped_rows,
        },
    )