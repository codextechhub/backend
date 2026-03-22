from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import (
    ImportBatchStatusChoices,
    ImportValidationIssue,
    ValidationSeverityChoices,
)
from ..validators import (
    find_duplicate_values,
    summarize_issues,
    validate_duplicate_headers,
    validate_email,
    validate_required_columns,
    validate_required_fields_for_row,
)


DATASET_REQUIRED_COLUMNS = {
    "students": ["full_name", "admission_number", "email"],
    "staff": ["full_name", "employee_id", "email"],
    "classes": ["class_name", "arm"],
    "fees": ["fee_name", "amount", "term"],
}

DATASET_REQUIRED_ROW_FIELDS = {
    "students": ["full_name", "admission_number"],
    "staff": ["full_name", "employee_id"],
    "classes": ["class_name"],
    "fees": ["fee_name", "amount"],
}


def _save_validation_issues(import_batch, issues: list[dict]):
    """
    Replace previous validation issues with fresh ones.
    """
    import_batch.validation_issues.all().delete()

    objects = []
    for issue in issues:
        objects.append(
            ImportValidationIssue(
                import_batch=import_batch,
                severity=issue.get("severity", ValidationSeverityChoices.ERROR),
                code=issue.get("code", "business_rule"),
                message=issue.get("message", ""),
                help_text=issue.get("help_text", ""),
                row_number=issue.get("row_number"),
                column_name=issue.get("column_name", ""),
                field_name=issue.get("field_name", ""),
                raw_value="" if issue.get("raw_value") is None else str(issue.get("raw_value")),
                normalized_value="" if issue.get("normalized_value") is None else str(issue.get("normalized_value")),
                metadata=issue.get("metadata", {}),
            )
        )

    if objects:
        ImportValidationIssue.objects.bulk_create(objects)


def _validate_headers(import_batch) -> list[dict]:
    issues = []

    headers = import_batch.detected_columns or []
    required_columns = DATASET_REQUIRED_COLUMNS.get(import_batch.dataset_type, [])

    issues.extend(validate_duplicate_headers(headers))
    issues.extend(validate_required_columns(headers, required_columns))

    return issues


def _validate_preview_rows(import_batch) -> list[dict]:
    issues = []

    rows = import_batch.preview_rows or []
    required_fields = DATASET_REQUIRED_ROW_FIELDS.get(import_batch.dataset_type, [])

    for row_number, row in enumerate(rows, start=1):
        issues.extend(
            validate_required_fields_for_row(
                row_data=row,
                required_fields=required_fields,
                row_number=row_number,
            )
        )

        if "email" in row:
            email_issue = validate_email(
                value=row.get("email"),
                row_number=row_number,
                column_name="email",
            )
            if email_issue:
                issues.append(email_issue)

    if import_batch.dataset_type == "students":
        issues.extend(find_duplicate_values(rows, "admission_number"))
        issues.extend(find_duplicate_values(rows, "email"))

    if import_batch.dataset_type == "staff":
        issues.extend(find_duplicate_values(rows, "employee_id"))
        issues.extend(find_duplicate_values(rows, "email"))

    return issues


@transaction.atomic
def validate_import_batch(import_batch) -> dict:
    """
    Main validator for one import batch.
    """
    import_batch.status = ImportBatchStatusChoices.VALIDATING
    import_batch.validation_started_at = timezone.now()
    import_batch.save(update_fields=["status", "validation_started_at", "updated_at"])

    issues = []
    issues.extend(_validate_headers(import_batch))
    issues.extend(_validate_preview_rows(import_batch))

    summary = summarize_issues(issues)

    _save_validation_issues(import_batch, issues)

    import_batch.validation_summary = summary
    import_batch.validation_completed_at = timezone.now()
    import_batch.has_critical_errors = summary["error_count"] > 0
    import_batch.is_ready_for_import = summary["error_count"] == 0
    import_batch.status = (
        ImportBatchStatusChoices.READY_TO_IMPORT
        if summary["error_count"] == 0
        else ImportBatchStatusChoices.VALIDATION_FAILED
    )
    import_batch.save(
        update_fields=[
            "validation_summary",
            "validation_completed_at",
            "has_critical_errors",
            "is_ready_for_import",
            "status",
            "updated_at",
        ]
    )

    return {
        "summary": summary,
        "issues": issues,
    }