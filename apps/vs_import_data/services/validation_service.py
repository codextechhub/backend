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
    Columns are fetched once and passed to each row validator to avoid N+1 queries.
    """
    issues = []

    if not import_batch.template:
        return issues

    rows = import_batch.preview_rows or []
    columns = list(import_batch.template.columns.order_by("column_order"))

    for row_number, row_data in enumerate(rows, start=1):
        issues.extend(
            validate_row_against_template(
                row_data=row_data,
                row_number=row_number,
                columns=columns,
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


def _validate_template_rules(import_batch) -> list[dict]:
    """
    Apply dataset-level rules from template.validation_rules JSON.

    Supported keys:
      min_rows (int)  — file must contain at least this many data rows.
      max_rows (int)  — file must contain no more than this many data rows.
    """
    issues = []

    if not import_batch.template:
        return issues

    rules = import_batch.template.validation_rules or {}
    if not rules:
        return issues

    rows = import_batch.preview_rows or []
    row_count = len(rows)

    min_rows = rules.get("min_rows")
    if min_rows is not None:
        try:
            min_rows = int(min_rows)
            if row_count < min_rows:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": (
                        f"File must contain at least {min_rows} data "
                        f"{'row' if min_rows == 1 else 'rows'} (found {row_count})."
                    ),
                })
        except (TypeError, ValueError):
            pass

    max_rows = rules.get("max_rows")
    if max_rows is not None:
        try:
            max_rows = int(max_rows)
            if row_count > max_rows:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": (
                        f"File cannot contain more than {max_rows} data "
                        f"{'row' if max_rows == 1 else 'rows'} (found {row_count})."
                    ),
                })
        except (TypeError, ValueError):
            pass

    return issues


def _build_col_resolver(template):
    """
    Returns a callable that maps a target_field name to the CSV column_name
    defined on the template, falling back to the target_field itself.
    Keeps dataset validators decoupled from hard-coded column header strings.
    """
    mapping = {c.target_field: c.column_name for c in template.columns.all()}
    return lambda target_field: mapping.get(target_field, target_field)


def _validate_dataset_specific_rules(import_batch) -> list[dict]:
    """
    Dataset-specific business rules that catch runtime errors before execution.
    Routed by template.dataset_type.
    """
    if not import_batch.template:
        return []
    dataset_type = import_batch.template.dataset_type
    if dataset_type == "schools":
        return _validate_schools_rules(import_batch)
    if dataset_type == "branches":
        return _validate_branches_rules(import_batch)
    return []


def _validate_schools_rules(import_batch) -> list[dict]:
    from datetime import date as date_type
    from django.utils.text import slugify
    from vs_schools.models import RESERVED_TENANT_SLUGS, PackagePlan, School, XVSModules
    from vs_user.models import User

    issues = []
    rows = import_batch.preview_rows or []

    _col = _build_col_resolver(import_batch.template)

    # Prefetch valid plans (with limits) and module keys once to avoid per-row DB hits
    active_plans = {p.code: p for p in PackagePlan.objects.filter(is_active=True)}
    valid_plan_codes = set(active_plans.keys())
    valid_module_keys = set(XVSModules.objects.filter(is_active=True).values_list("key", flat=True))
    existing_slugs = set(School.objects.values_list("slug", flat=True))
    today = timezone.now().date()

    # Track within-file duplicates
    seen_admin_emails: dict[str, int] = {}
    seen_slugs: dict[str, int] = {}  # resolved_slug -> first row_number that claims it

    for row_number, row in enumerate(rows, start=1):
        def _s(target_field: str) -> str:
            return (row.get(_col(target_field)) or "").strip()

        def _int(target_field: str):
            try:
                v = row.get(_col(target_field))
                return int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        # --- Slug: resolve the same way SchoolCreateSerializer does ---
        raw_slug = _s("slug")
        raw_name = _s("name")
        resolved_slug = slugify(raw_slug) if raw_slug else slugify(raw_name)
        slug_col = _col("slug") if raw_slug else _col("name")

        if resolved_slug:
            # Mirror serializer: auto-generated reserved slug gets "-school" appended
            effective_slug = (
                f"{resolved_slug}-school"
                if not raw_slug and resolved_slug in RESERVED_TENANT_SLUGS
                else resolved_slug
            )

            if raw_slug and effective_slug in RESERVED_TENANT_SLUGS:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": f"Slug '{effective_slug}' is reserved and cannot be used.",
                    "row_number": row_number,
                    "column_name": _col("slug"),
                    "raw_value": raw_slug,
                })
            elif effective_slug in existing_slugs:
                suggestions = [
                    f"{effective_slug}-{i}" for i in range(2, 8)
                    if f"{effective_slug}-{i}" not in existing_slugs
                    and f"{effective_slug}-{i}" not in seen_slugs
                ][:5]
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": f"School with slug '{effective_slug}' already exists in the database.",
                    "row_number": row_number,
                    "column_name": slug_col,
                    "raw_value": raw_slug or raw_name,
                    "metadata": {"suggestions": suggestions},
                })
            elif effective_slug in seen_slugs:
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": (
                        f"Slug '{effective_slug}' conflicts with row {seen_slugs[effective_slug]}. "
                        "Each school must resolve to a unique slug."
                    ),
                    "row_number": row_number,
                    "column_name": slug_col,
                    "raw_value": raw_slug or raw_name,
                })
            else:
                seen_slugs[effective_slug] = row_number

        # --- package_plan: must exist and be active ---
        plan_code = _s("package_plan")
        if plan_code and plan_code not in valid_plan_codes:
            issues.append({
                "severity": "error",
                "code": "invalid_choice",
                "message": f"Package plan '{plan_code}' does not exist or is not active.",
                "row_number": row_number,
                "column_name": _col("package_plan"),
                "raw_value": plan_code,
            })

        # --- capacity checks (plan present and valid) ---
        elif plan_code and plan_code in active_plans:
            plan = active_plans[plan_code]
            student_cap = _int("student_capacity")
            teacher_cap = _int("teacher_capacity")
            admin_cap = _int("admin_capacity")

            # min_value=1 (mirrors serializer IntegerField(min_value=1))
            for cap_val, target_field in (
                (student_cap, "student_capacity"),
                (teacher_cap, "teacher_capacity"),
                (admin_cap, "admin_capacity"),
            ):
                if cap_val is not None and cap_val < 1:
                    issues.append({
                        "severity": "error",
                        "code": "business_rule",
                        "message": f"{_col(target_field)} must be at least 1.",
                        "row_number": row_number,
                        "column_name": _col(target_field),
                        "raw_value": str(cap_val),
                    })

            # plan limits
            if student_cap is not None and student_cap >= 1 and plan.max_students is not None and student_cap > plan.max_students:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": f"Exceeds plan limit of {plan.max_students} students.",
                    "row_number": row_number,
                    "column_name": _col("student_capacity"),
                    "raw_value": str(student_cap),
                })
            if teacher_cap is not None and teacher_cap >= 1 and plan.max_teachers is not None and teacher_cap > plan.max_teachers:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": f"Exceeds plan limit of {plan.max_teachers} teachers.",
                    "row_number": row_number,
                    "column_name": _col("teacher_capacity"),
                    "raw_value": str(teacher_cap),
                })
            if admin_cap is not None and admin_cap >= 1 and plan.max_admins is not None and admin_cap > plan.max_admins:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": f"Exceeds plan limit of {plan.max_admins} admins.",
                    "row_number": row_number,
                    "column_name": _col("admin_capacity"),
                    "raw_value": str(admin_cap),
                })

        # --- enabled_modules: each key must exist and be active ---
        raw_modules = _s("enabled_modules")
        if raw_modules:
            for key in [m.strip() for m in raw_modules.split(",") if m.strip()]:
                if key not in valid_module_keys:
                    issues.append({
                        "severity": "error",
                        "code": "invalid_choice",
                        "message": f"Module key '{key}' does not exist or is not active.",
                        "row_number": row_number,
                        "column_name": _col("enabled_modules"),
                        "raw_value": raw_modules,
                    })

        # --- subscription_expires_at: YYYY-MM-DD and must be future ---
        expires_raw = _s("subscription_expires_at")
        if expires_raw:
            try:
                expires_date = date_type.fromisoformat(expires_raw)
                if expires_date <= today:
                    issues.append({
                        "severity": "error",
                        "code": "business_rule",
                        "message": f"subscription_expires_at must be a future date (got '{expires_raw}').",
                        "row_number": row_number,
                        "column_name": _col("subscription_expires_at"),
                        "raw_value": expires_raw,
                    })
            except ValueError:
                issues.append({
                    "severity": "error",
                    "code": "invalid_format",
                    "message": f"subscription_expires_at must be in YYYY-MM-DD format (got '{expires_raw}').",
                    "row_number": row_number,
                    "column_name": _col("subscription_expires_at"),
                    "raw_value": expires_raw,
                })

        # --- admin full_name: must not be empty/whitespace-only ---
        for name_target, email_target in (
            ("school_admin_full_name", "school_admin_email"),
            ("branch_admin_full_name", "branch_admin_email"),
        ):
            # Only enforce when the corresponding email is present (admin is being created)
            if _s(email_target) and not _s(name_target):
                issues.append({
                    "severity": "error",
                    "code": "required_value_missing",
                    "message": f"{_col(name_target)} is required when {_col(email_target)} is provided.",
                    "row_number": row_number,
                    "column_name": _col(name_target),
                    "raw_value": "",
                })

        # --- admin emails: must not already exist as users, must not repeat across rows ---
        for email_target in ("school_admin_email", "branch_admin_email"):
            email = _s(email_target).lower()
            if not email:
                continue
            if User.objects.filter(email=email).exists():
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": f"A user with email '{email}' already exists.",
                    "row_number": row_number,
                    "column_name": _col(email_target),
                    "raw_value": email,
                })
            elif email in seen_admin_emails:
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": (
                        f"Email '{email}' is already used in row {seen_admin_emails[email]}. "
                        "Each admin email must be unique across rows."
                    ),
                    "row_number": row_number,
                    "column_name": _col(email_target),
                    "raw_value": email,
                })
            else:
                seen_admin_emails[email] = row_number

    return issues


def _validate_branches_rules(import_batch) -> list[dict]:
    from vs_schools.models import School
    from vs_user.models import User

    issues = []
    rows = import_batch.preview_rows or []
    seen_admin_emails: dict[str, int] = {}
    # school_slug (or "batch-scoped") -> row_number of first is_main=TRUE row
    seen_main_branch: dict[str, int] = {}

    _col = _build_col_resolver(import_batch.template)

    for row_number, row in enumerate(rows, start=1):
        def _s(target_field: str) -> str:
            return (row.get(_col(target_field)) or "").strip()

        # --- school resolution: required when batch is not school-scoped ---
        if import_batch.school is None:
            school_slug = _s("school_slug")
            school_code = _s("school_code")
            if school_slug:
                if not School.objects.filter(slug=school_slug).exists():
                    issues.append({
                        "severity": "error",
                        "code": "cross_reference_missing",
                        "message": f"No school found with slug '{school_slug}'.",
                        "row_number": row_number,
                        "column_name": _col("school_slug"),
                        "raw_value": school_slug,
                    })
            elif school_code:
                if not School.objects.filter(code=school_code).exists():
                    issues.append({
                        "severity": "error",
                        "code": "cross_reference_missing",
                        "message": f"No school found with code '{school_code}'.",
                        "row_number": row_number,
                        "column_name": _col("school_code"),
                        "raw_value": school_code,
                    })
            else:
                issues.append({
                    "severity": "error",
                    "code": "required_value_missing",
                    "message": f"Either {_col('school_slug')} or {_col('school_code')} is required when the batch is not school-scoped.",
                    "row_number": row_number,
                    "column_name": _col("school_slug"),
                    "raw_value": "",
                })

        # --- is_main: only one TRUE per school allowed (within-file + DB) ---
        school_key = _s("school_slug") or _s("school_code") or "__batch_scoped__"
        is_main_raw = _s("is_main").lower()
        if is_main_raw in ("true", "1", "yes"):
            if school_key in seen_main_branch:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": (
                        f"Row {seen_main_branch[school_key]} already marks a main branch for "
                        f"school '{school_key}'. Only one branch per school can be Is Main Branch = TRUE."
                    ),
                    "row_number": row_number,
                    "column_name": _col("is_main"),
                    "raw_value": _s("is_main"),
                })
            else:
                seen_main_branch[school_key] = row_number
                # Also check the DB — school may already have a main branch
                if import_batch.school is not None:
                    check_school = import_batch.school
                else:
                    slug_val = _s("school_slug")
                    code_val = _s("school_code")
                    check_school = (
                        School.objects.filter(slug=slug_val).first() if slug_val
                        else School.objects.filter(code=code_val).first() if code_val
                        else None
                    )
                from vs_schools.models import Branch as BranchModel
                if check_school and BranchModel.objects.filter(school=check_school, is_main=True).exists():
                    issues.append({
                        "severity": "error",
                        "code": "business_rule",
                        "message": f"School '{school_key}' already has a main branch in the database.",
                        "row_number": row_number,
                        "column_name": _col("is_main"),
                        "raw_value": _s("is_main"),
                    })

        # --- branch_admin_full_name: required when email is present ---
        if _s("branch_admin_email") and not _s("branch_admin_full_name"):
            issues.append({
                "severity": "error",
                "code": "required_value_missing",
                "message": f"{_col('branch_admin_full_name')} is required when {_col('branch_admin_email')} is provided.",
                "row_number": row_number,
                "column_name": _col("branch_admin_full_name"),
                "raw_value": "",
            })

        # --- branch_admin_email: must not already exist, must be unique across rows ---
        email = _s("branch_admin_email").lower()
        if email:
            if User.objects.filter(email=email).exists():
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": f"A user with email '{email}' already exists.",
                    "row_number": row_number,
                    "column_name": _col("branch_admin_email"),
                    "raw_value": email,
                })
            elif email in seen_admin_emails:
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": (
                        f"Email '{email}' is already used in row {seen_admin_emails[email]}. "
                        "Each branch admin email must be unique across rows."
                    ),
                    "row_number": row_number,
                    "column_name": _col("branch_admin_email"),
                    "raw_value": email,
                })
            else:
                seen_admin_emails[email] = row_number

    return issues


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
    error_count = summary["error_count"]
    error_rows = summary.get("error_rows", error_count)
    valid_rows = import_batch.total_rows - error_rows

    import_batch.validation_summary = summary
    import_batch.validation_completed_at = timezone.now()
    import_batch.has_critical_errors = error_count > 0
    import_batch.is_ready_for_import = valid_rows > 0
    import_batch.structure_matches_template = error_count == 0

    import_batch.status = (
        ImportBatchStatusChoices.READY_TO_IMPORT
        if valid_rows > 0
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
    issues.extend(_validate_template_rules(import_batch))
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