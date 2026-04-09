from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Iterable


# =========================================================
# Generic helper functions
# =========================================================
def normalize_string(value) -> str:
    """
    Convert a value to a clean string for comparison.
    Useful for duplicate checks and required field checks.
    """
    if value is None:
        return ""
    return str(value).strip()


def is_empty(value) -> bool:
    """
    Returns True if the value is empty.
    Treats None, empty string, and whitespace-only string as empty.
    """
    return normalize_string(value) == ""


def normalize_header(value: str) -> str:
    """
    Normalizes a column/header name so matching is easier.

    Example:
        ' Student Name ' -> 'student_name'
        'Date of Birth' -> 'date_of_birth'
    """
    return normalize_string(value).lower().replace(" ", "_")


# =========================================================
# File-level validators
# =========================================================
def validate_allowed_file_extension(filename: str, allowed_extensions: Iterable[str] | None = None) -> None:
    """
    Validate that the uploaded file has a supported extension.
    """
    if allowed_extensions is None:
        allowed_extensions = {".csv", ".xlsx", ".xls"}

    filename = normalize_string(filename).lower()
    matched = any(filename.endswith(ext) for ext in allowed_extensions)

    if not matched:
        raise ValueError("Only .csv, .xlsx, and .xls files are allowed.")


def validate_file_not_empty(rows: list[dict]) -> None:
    """
    Ensure the uploaded file has at least one row of usable data.
    """
    if not rows:
        raise ValueError("The uploaded file is empty.")


def validate_sheet_name_provided_for_excel(file_format: str, sheet_name: str | None) -> None:
    """
    If the uploaded file is Excel, a sheet name may be required
    depending on your business rule.
    """
    if file_format in {"xlsx", "xls"} and is_empty(sheet_name):
        raise ValueError("A sheet name is required for Excel imports.")


# =========================================================
# Header / structure validators
# =========================================================
def validate_required_columns(headers: list[str], required_columns: list[str]) -> list[dict]:
    """
    Check whether all required columns are present.

    Returns a list of issue dictionaries instead of raising immediately.
    That makes it easier to build a validation report.
    """
    issues = []

    normalized_headers = {normalize_header(h) for h in headers}

    for required in required_columns:
        if normalize_header(required) not in normalized_headers:
            issues.append(
                {
                    "severity": "error",
                    "code": "column_missing",
                    "message": f"Required column '{required}' is missing.",
                    "column_name": required,
                }
            )

    return issues


def validate_unknown_columns(headers: list[str], allowed_columns: list[str]) -> list[dict]:
    """
    Identify columns that are not expected for this dataset.
    """
    issues = []

    normalized_allowed = {normalize_header(c) for c in allowed_columns}

    for header in headers:
        if normalize_header(header) not in normalized_allowed:
            issues.append(
                {
                    "severity": "warning",
                    "code": "column_unknown",
                    "message": f"Column '{header}' is not recognized.",
                    "column_name": header,
                }
            )

    return issues


def validate_duplicate_headers(headers: list[str]) -> list[dict]:
    """
    Detect duplicate column names in the uploaded file.
    """
    issues = []

    normalized_headers = [normalize_header(h) for h in headers]
    counts = Counter(normalized_headers)

    for original_header in headers:
        key = normalize_header(original_header)
        if counts[key] > 1:
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_mapping",
                    "message": f"Column '{original_header}' appears more than once.",
                    "column_name": original_header,
                }
            )

    return issues


# =========================================================
# Required value validators
# =========================================================
def validate_required_value(
    value,
    row_number: int,
    column_name: str,
) -> dict | None:
    """
    Validate that one required cell has a value.
    """
    if is_empty(value):
        return {
            "severity": "error",
            "code": "required_value_missing",
            "message": f"'{column_name}' is required.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }
    return None


def validate_required_fields_for_row(
    row_data: dict,
    required_fields: list[str],
    row_number: int,
) -> list[dict]:
    """
    Check that all required fields for one row are filled.
    """
    issues = []

    for field in required_fields:
        issue = validate_required_value(
            value=row_data.get(field),
            row_number=row_number,
            column_name=field,
        )
        if issue:
            issues.append(issue)

    return issues


# =========================================================
# Type / format validators
# =========================================================
def validate_email(value, row_number: int, column_name: str = "email") -> dict | None:
    """
    Simple email validator.
    Keep it basic here; deeper email rules can be added later.
    """
    value = normalize_string(value)

    if value == "":
        return None

    if "@" not in value or "." not in value.split("@")[-1]:
        return {
            "severity": "error",
            "code": "invalid_format",
            "message": f"'{column_name}' must be a valid email address.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }

    return None


def validate_integer(value, row_number: int, column_name: str) -> dict | None:
    """
    Validate that a value can be converted to an integer.
    """
    if is_empty(value):
        return None

    try:
        int(str(value).strip())
        return None
    except (ValueError, TypeError):
        return {
            "severity": "error",
            "code": "invalid_format",
            "message": f"'{column_name}' must be a whole number.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }


def validate_decimal(value, row_number: int, column_name: str) -> dict | None:
    """
    Validate that a value can be converted to decimal.
    Useful for fees, balances, amounts, scores, etc.
    """
    if is_empty(value):
        return None

    try:
        Decimal(str(value).strip())
        return None
    except (InvalidOperation, ValueError, TypeError):
        return {
            "severity": "error",
            "code": "invalid_format",
            "message": f"'{column_name}' must be a valid number.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }


def validate_boolean(value, row_number: int, column_name: str) -> dict | None:
    """
    Validate common boolean-like values.
    Accepted examples: yes/no, true/false, 1/0
    """
    if is_empty(value):
        return None

    normalized = normalize_string(value).lower()
    allowed = {"true", "false", "yes", "no", "1", "0"}

    if normalized not in allowed:
        return {
            "severity": "error",
            "code": "invalid_choice",
            "message": f"'{column_name}' must be one of: true, false, yes, no, 1, 0.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }

    return None


def validate_choice(value, allowed_values: list[str], row_number: int, column_name: str) -> dict | None:
    """
    Validate that a value belongs to a list of allowed choices.
    """
    if is_empty(value):
        return None

    normalized_value = normalize_string(value).lower()
    normalized_allowed = [normalize_string(v).lower() for v in allowed_values]

    if normalized_value not in normalized_allowed:
        return {
            "severity": "error",
            "code": "invalid_choice",
            "message": f"'{column_name}' must be one of: {', '.join(allowed_values)}.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }

    return None


def validate_max_length(value, max_length: int, row_number: int, column_name: str) -> dict | None:
    """
    Validate that text does not exceed a maximum length.
    """
    value = normalize_string(value)

    if value and len(value) > max_length:
        return {
            "severity": "error",
            "code": "invalid_format",
            "message": f"'{column_name}' cannot be longer than {max_length} characters.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }

    return None


# =========================================================
# Duplicate validators
# =========================================================
def find_duplicate_values(
    rows: list[dict],
    field_name: str,
) -> list[dict]:
    """
    Find duplicate values in one field across the uploaded rows.

    Example:
        duplicate emails
        duplicate admission numbers
        duplicate employee IDs
    """
    issues = []

    seen = {}
    for index, row in enumerate(rows, start=1):
        value = normalize_string(row.get(field_name))
        if value == "":
            continue

        key = value.lower()

        if key in seen:
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": f"Duplicate value '{value}' found for '{field_name}'.",
                    "row_number": index,
                    "column_name": field_name,
                    "raw_value": value,
                    "metadata": {
                        "first_seen_row": seen[key],
                    },
                }
            )
        else:
            seen[key] = index

    return issues


def find_duplicate_composite_values(
    rows: list[dict],
    field_names: list[str],
) -> list[dict]:
    """
    Find duplicates using a combination of fields.

    Example:
        first_name + last_name + date_of_birth
        class_name + arm + session
    """
    issues = []

    seen = {}

    for index, row in enumerate(rows, start=1):
        values = [normalize_string(row.get(field)) for field in field_names]

        if all(v == "" for v in values):
            continue

        key = tuple(v.lower() for v in values)

        if key in seen:
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": f"Duplicate combination found for fields: {', '.join(field_names)}.",
                    "row_number": index,
                    "metadata": {
                        "fields": field_names,
                        "values": values,
                        "first_seen_row": seen[key],
                    },
                }
            )
        else:
            seen[key] = index

    return issues


# =========================================================
# Cross-reference validators
# =========================================================
def validate_foreign_key_reference(
    value,
    valid_lookup_values: set[str],
    row_number: int,
    column_name: str,
    reference_name: str,
) -> dict | None:
    """
    Check whether a value exists in a known lookup set.

    Example:
        class_name exists
        school branch exists
        fee category exists
        department exists
    """
    value = normalize_string(value)

    if value == "":
        return None

    normalized_lookup = {normalize_string(v).lower() for v in valid_lookup_values}

    if value.lower() not in normalized_lookup:
        return {
            "severity": "error",
            "code": "cross_reference_missing",
            "message": f"'{value}' in '{column_name}' does not match any existing {reference_name}.",
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": value,
        }

    return None


# =========================================================
# Business rule validators
# =========================================================
def validate_start_end_order(
    start_value,
    end_value,
    row_number: int,
    start_column: str,
    end_column: str,
) -> dict | None:
    """
    Basic rule validator for checking order of comparable values.

    Works best when values are already parsed before being passed in.
    Example:
        start_date <= end_date
        min_score <= max_score
    """
    if start_value is None or end_value is None:
        return None

    if start_value > end_value:
        return {
            "severity": "error",
            "code": "business_rule",
            "message": f"'{start_column}' cannot be greater than '{end_column}'.",
            "row_number": row_number,
            "metadata": {
                "start_column": start_column,
                "end_column": end_column,
                "start_value": str(start_value),
                "end_value": str(end_value),
            },
        }

    return None


def validate_min_not_greater_than_max(
    min_value,
    max_value,
    row_number: int,
    min_column: str,
    max_column: str,
) -> dict | None:
    """
    Explicit validator for min/max values.
    """
    if min_value is None or max_value is None:
        return None

    if min_value > max_value:
        return {
            "severity": "error",
            "code": "business_rule",
            "message": f"'{min_column}' cannot be greater than '{max_column}'.",
            "row_number": row_number,
            "metadata": {
                "min_column": min_column,
                "max_column": max_column,
                "min_value": str(min_value),
                "max_value": str(max_value),
            },
        }

    return None


# =========================================================
# Mapping validators
# =========================================================
def validate_mapping_targets_unique(mappings: list[dict]) -> list[dict]:
    """
    Check whether the same target field has been mapped more than once.
    Useful when one target field should only come from one source column.
    """
    issues = []

    seen = {}

    for item in mappings:
        source_column = normalize_string(item.get("source_column"))
        target_field = normalize_string(item.get("target_field"))

        if not target_field:
            continue

        key = target_field.lower()

        if key in seen:
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_mapping",
                    "message": f"Target field '{target_field}' has been mapped more than once.",
                    "metadata": {
                        "first_source_column": seen[key],
                        "duplicate_source_column": source_column,
                        "target_field": target_field,
                    },
                }
            )
        else:
            seen[key] = source_column

    return issues


def validate_required_mappings_present(
    mappings: list[dict],
    required_target_fields: list[str],
) -> list[dict]:
    """
    Ensure all required target fields are mapped before validation/import.
    """
    issues = []

    mapped_targets = {
        normalize_string(item.get("target_field")).lower()
        for item in mappings
        if normalize_string(item.get("target_field"))
    }

    for field in required_target_fields:
        if normalize_string(field).lower() not in mapped_targets:
            issues.append(
                {
                    "severity": "error",
                    "code": "column_missing",
                    "message": f"Required target field '{field}' has not been mapped.",
                    "field_name": field,
                }
            )

    return issues


# =========================================================
# Summary helpers
# =========================================================
def summarize_issues(issues: list[dict]) -> dict:
    """
    Create a simple summary from a list of validation issues.
    """
    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    warning_count = sum(1 for issue in issues if issue.get("severity") == "warning")
    info_count = sum(1 for issue in issues if issue.get("severity") == "info")

    return {
        "total_issues": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "has_critical_errors": error_count > 0,
    }