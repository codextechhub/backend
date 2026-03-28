from __future__ import annotations

from ..validators import (
    validate_required_value,
    validate_email,
    validate_integer,
    validate_decimal,
    validate_boolean,
    validate_choice,
    validate_max_length,
)


def compare_uploaded_headers_to_template(uploaded_headers: list[str], template) -> list[dict]:
    """
    Validate uploaded headers against official template headers.
    """
    issues = []

    expected_columns = list(template.columns.order_by("column_order"))
    expected_headers = [col.column_name for col in expected_columns]

    uploaded_set = set(uploaded_headers)
    expected_set = set(expected_headers)

    missing = expected_set - uploaded_set
    extra = uploaded_set - expected_set

    for header in sorted(missing):
        issues.append(
            {
                "severity": "error",
                "code": "column_missing",
                "message": f"Required template column '{header}' is missing.",
                "column_name": header,
            }
        )

    for header in sorted(extra):
        issues.append(
            {
                "severity": "warning",
                "code": "column_unknown",
                "message": f"Uploaded column '{header}' is not part of the official template.",
                "column_name": header,
            }
        )

    return issues


def validate_row_against_template(row_data: dict, row_number: int, template) -> list[dict]:
    """
    Validate one uploaded row using ImportTemplateColumn definitions.
    """
    issues = []

    columns = list(template.columns.order_by("column_order"))

    for col in columns:
        value = row_data.get(col.column_name)

        if col.is_required:
            issue = validate_required_value(value, row_number, col.column_name)
            if issue:
                issues.append(issue)
                continue

        if value in [None, ""]:
            continue

        if col.data_type == "email":
            issue = validate_email(value, row_number, col.column_name)
            if issue:
                issues.append(issue)

        elif col.data_type == "integer":
            issue = validate_integer(value, row_number, col.column_name)
            if issue:
                issues.append(issue)

        elif col.data_type == "decimal":
            issue = validate_decimal(value, row_number, col.column_name)
            if issue:
                issues.append(issue)

        elif col.data_type == "boolean":
            issue = validate_boolean(value, row_number, col.column_name)
            if issue:
                issues.append(issue)

        elif col.data_type == "choice":
            issue = validate_choice(value, col.allowed_values or [], row_number, col.column_name)
            if issue:
                issues.append(issue)

        if col.max_length:
            issue = validate_max_length(value, col.max_length, row_number, col.column_name)
            if issue:
                issues.append(issue)

    return issues