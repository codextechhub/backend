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
    validate_foreign_key_reference,
)
from .template import get_template_headers
from .template_validation import (
    compare_uploaded_headers_to_template,
    validate_row_against_template,
)


# =========================================================
# Internal helpers
# =========================================================
def _save_validation_issues(import_batch, issues: list[dict]) -> None:
    """
    Replace old validation issues with the latest run.
    """
    import_batch.validation_issues.all().delete()

    issue_objects = []

    for issue in issues:
        issue_objects.append(
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

    if issue_objects:
        ImportValidationIssue.objects.bulk_create(issue_objects)


def _validate_template_presence(import_batch) -> list[dict]:
    """
    Ensure a template is attached to the batch.
    """
    issues = []

    if not import_batch.template:
        issues.append(
            {
                "severity": "error",
                "code": "business_rule",
                "message": "No import template is attached to this batch.",
            }
        )

    return issues


def _validate_headers_against_template(import_batch) -> list[dict]:
    """
    Validate uploaded headers against the selected official template.
    """
    issues = []

    uploaded_headers = import_batch.uploaded_headers or []

    issues.extend(validate_duplicate_headers(uploaded_headers))

    if import_batch.template:
        issues.extend(
            compare_uploaded_headers_to_template(
                uploaded_headers=uploaded_headers,
                template=import_batch.template,
            )
        )

    return issues


def _validate_rows_against_template(import_batch) -> list[dict]:
    """
    Validate each preview row using ImportTemplateColumn definitions.
    """
    issues = []

    if not import_batch.template:
        return issues

    rows = import_batch.preview_rows or []

    for row_number, row_data in enumerate(rows, start=1):
        issues.extend(
            validate_row_against_template(
                row_data=row_data,
                row_number=row_number,
                template=import_batch.template,
            )
        )

    return issues


def _validate_template_uniqueness_rules(import_batch) -> list[dict]:
    """
    Enforce any template columns marked as unique within the uploaded file.
    """
    issues = []

    if not import_batch.template:
        return issues

    rows = import_batch.preview_rows or []
    unique_columns = import_batch.template.columns.filter(is_unique=True)

    for template_column in unique_columns:
        issues.extend(find_duplicate_values(rows, template_column.column_name))

    return issues


def _validate_dataset_specific_rules(import_batch) -> list[dict]:
    """
    Dataset-specific business rules beyond column-level validation.
    Schools: all validation is handled by SchoolCreateSerializer at execution time.
    """
    return []


def _resolve_model(name: str):
    """
    Find a Django model class by name (case-insensitive) across all installed apps.
    Returns None if no match is found.
    """
    from django.apps import apps
    name_lower = name.lower()
    for model in apps.get_models():
        if model.__name__.lower() == name_lower:
            return model
    return None


def _validate_cross_references(import_batch) -> list[dict]:
    """
    For each template column that declares a reference_model + reference_lookup_field,
    fetch the set of valid values from that model (scoped to the batch's school/branch
    where the model supports it) and flag any row value that doesn't match.
    """
    issues = []

    if not import_batch.template:
        return issues

    rows = import_batch.preview_rows or []
    if not rows:
        return issues

    ref_columns = list(
        import_batch.template.columns
        .exclude(reference_model="")
        .exclude(reference_lookup_field="")
    )
    if not ref_columns:
        return issues

    for col in ref_columns:
        model_class = _resolve_model(col.reference_model)
        if model_class is None:
            continue

        qs = model_class.objects.all()

        # Scope to school or branch when the referenced model supports it
        field_names = {f.name for f in model_class._meta.get_fields()}
        if "school" in field_names and import_batch.school_id:
            qs = qs.filter(school_id=import_batch.school_id)
        elif "branch" in field_names and import_batch.branch_id:
            qs = qs.filter(branch_id=import_batch.branch_id)

        try:
            valid_values = set(qs.values_list(col.reference_lookup_field, flat=True))
        except Exception:
            continue

        for row_number, row_data in enumerate(rows, start=1):
            issue = validate_foreign_key_reference(
                value=row_data.get(col.column_name),
                valid_lookup_values=valid_values,
                row_number=row_number,
                column_name=col.column_name,
                reference_name=col.reference_model,
            )
            if issue:
                issues.append(issue)

    return issues


def _update_batch_validation_state(import_batch, summary: dict) -> None:
    """
    Update validation result fields on the batch.
    """
    import_batch.validation_summary = summary
    import_batch.validation_completed_at = timezone.now()
    import_batch.has_critical_errors = summary["error_count"] > 0
    import_batch.is_ready_for_import = summary["error_count"] == 0
    import_batch.structure_matches_template = summary["error_count"] == 0

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
            "structure_matches_template",
            "status",
            "updated_at",
        ]
    )


# =========================================================
# Main validation entry point
# =========================================================
@transaction.atomic
def validate_import_batch(import_batch) -> dict:
    """
    Main validation function for a template-driven import batch.

    What it does:
    1. confirms a template exists
    2. validates uploaded headers against the template
    3. validates row values against template column rules
    4. checks uniqueness-in-file rules
    5. runs optional cross-reference and dataset-specific checks
    6. stores validation issues
    7. updates import batch status and summary
    """
    import_batch.status = ImportBatchStatusChoices.VALIDATING
    import_batch.validation_started_at = timezone.now()

    if import_batch.template:
        import_batch.template_headers_snapshot = get_template_headers(import_batch.template)

    import_batch.save(
        update_fields=[
            "status",
            "validation_started_at",
            "template_headers_snapshot",
            "updated_at",
        ]
    )

    issues = []

    issues.extend(_validate_template_presence(import_batch))
    issues.extend(_validate_headers_against_template(import_batch))
    issues.extend(_validate_rows_against_template(import_batch))
    issues.extend(_validate_template_uniqueness_rules(import_batch))
    issues.extend(_validate_cross_references(import_batch))
    issues.extend(_validate_dataset_specific_rules(import_batch))

    summary = summarize_issues(issues)

    _save_validation_issues(import_batch, issues)
    _update_batch_validation_state(import_batch, summary)

    return {
        "summary": summary,
        "issues": issues,
    }