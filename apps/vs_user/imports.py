"""Portable organogram imports owned by the platform user domain.

References use org-unit codes, position codes and role-template keys so files
can move between databases. Rows are upserted by those stable references, and
model validation remains the final authority for hierarchy and cycle rules.
"""
from __future__ import annotations

from vs_import_data.models import ImportRowActionChoices
from vs_import_data.services.import_executor import ImportExecutionResult

from .models import MatrixReport, OrgNode, Position


def _text(payload: dict, key: str) -> str:
    return str(payload.get(key) or "").strip()


def _boolean(payload: dict, key: str, *, default: bool = True) -> bool:
    raw = _text(payload, key)
    if not raw:
        return default
    folded = raw.casefold()
    if folded in {"true", "yes", "1"}:
        return True
    if folded in {"false", "no", "0"}:
        return False
    raise ValueError(f"{key.replace('_', ' ').title()} must be Yes or No.")


def _action(created: bool) -> str:
    return (
        ImportRowActionChoices.CREATE
        if created
        else ImportRowActionChoices.UPDATE
    )


def _org_code(raw: str, kind: str) -> str:
    """Normalize a portable org-unit code exactly as ``OrgNode.save`` does."""
    bare = raw
    for prefix in OrgNode._KIND_PREFIX.values():
        if bare.upper().startswith(prefix):
            bare = bare[len(prefix):]
            break
    return f"{OrgNode._KIND_PREFIX.get(kind, '')}{bare}"


def import_org_unit_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """Upsert one org unit after resolving its parent by portable code."""
    code = _text(payload, "code")
    kind = _text(payload, "kind").upper()
    code = _org_code(code, kind)
    parent_code = _text(payload, "parent_code")
    parent = None
    if parent_code:
        parent_kind = OrgNode._REQUIRED_PARENT_KIND.get(kind)
        parent_code = _org_code(parent_code, parent_kind)
        parent = OrgNode.objects.filter(code__iexact=parent_code).first()
        if parent is None:
            raise ValueError(
                f"Parent org unit '{parent_code}' does not exist yet. "
                "Import parents before their children."
            )

    instance = OrgNode.objects.filter(code__iexact=code).first()
    created = instance is None
    instance = instance or OrgNode(code=code)
    instance.name = _text(payload, "name")
    instance.kind = kind
    instance.parent = parent
    instance.description = _text(payload, "description")
    instance.is_active = _boolean(payload, "is_active")
    instance.full_clean()
    instance.save()
    return ImportExecutionResult(
        action=_action(created), instance=instance, target_model="OrgNode",
        message=f"Org unit '{instance.code}' imported successfully.",
    )


def import_position_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """Upsert one position after resolving every portable reference."""
    from vs_rbac.models import TenantRoleTemplate

    code = _text(payload, "code")
    unit_code = _text(payload, "org_unit_code")
    org_node = OrgNode.objects.filter(code__iexact=unit_code).first()
    if org_node is None:
        raise ValueError(f"Org unit '{unit_code}' does not exist.")

    manager_code = _text(payload, "reports_to_code")
    reports_to = None
    if manager_code:
        reports_to = Position.objects.filter(code__iexact=manager_code).first()
        if reports_to is None:
            raise ValueError(
                f"Reports-to position '{manager_code}' does not exist yet. "
                "Import manager positions before their reports."
            )

    role_key = _text(payload, "default_role_key")
    default_role = None
    if role_key:
        default_role = TenantRoleTemplate.objects.filter(
            tenant=import_batch.tenant, key=role_key,
        ).first()
        if default_role is None:
            raise ValueError(
                f"Role template '{role_key}' does not exist in the platform tenant."
            )

    instance = Position.objects.filter(code__iexact=code).first()
    created = instance is None
    instance = instance or Position(code=code)
    instance.title = _text(payload, "title")
    instance.org_node = org_node
    instance.reports_to = reports_to
    instance.default_role = default_role
    try:
        instance.headcount = int(_text(payload, "headcount") or "1")
    except ValueError as exc:
        raise ValueError("Headcount must be a whole number.") from exc
    instance.is_active = _boolean(payload, "is_active")
    instance.full_clean()
    instance.save()
    return ImportExecutionResult(
        action=_action(created), instance=instance, target_model="Position",
        message=f"Position '{instance.code}' imported successfully.",
    )


def import_matrix_report_row(import_batch, payload: dict, queued_by) -> ImportExecutionResult:
    """Upsert one dotted reporting line by its unique pair of position codes."""
    position_code = _text(payload, "position_code")
    manager_code = _text(payload, "reports_to_code")
    position = Position.objects.filter(code__iexact=position_code).first()
    if position is None:
        raise ValueError(f"Position '{position_code}' does not exist.")
    reports_to = Position.objects.filter(code__iexact=manager_code).first()
    if reports_to is None:
        raise ValueError(f"Reports-to position '{manager_code}' does not exist.")

    instance = MatrixReport.objects.filter(
        position=position, reports_to=reports_to,
    ).first()
    created = instance is None
    instance = instance or MatrixReport(position=position, reports_to=reports_to)
    instance.relationship_label = _text(payload, "relationship_label")
    instance.full_clean()
    instance.save()
    return ImportExecutionResult(
        action=_action(created), instance=instance, target_model="MatrixReport",
        message=(
            f"Matrix line '{position.code}' to '{reports_to.code}' imported "
            "successfully."
        ),
    )


def validate_organogram_import(import_batch) -> list[dict]:
    """Catch dependency order and duplicate matrix pairs before execution."""
    dataset_type = import_batch.template.dataset_type
    columns = {
        column.target_field: column.column_name
        for column in import_batch.template.columns.all()
    }

    def value(row, field):
        return str(row.get(columns.get(field, field)) or "").strip()

    issues = []
    seen = set()
    if dataset_type == "org_units":
        available = {code.casefold() for code in OrgNode.objects.values_list("code", flat=True)}
        for number, row in enumerate(import_batch.preview_rows or [], start=1):
            kind = value(row, "kind").upper()
            code = _org_code(value(row, "code"), kind)
            parent = value(row, "parent_code")
            if parent:
                parent = _org_code(parent, OrgNode._REQUIRED_PARENT_KIND.get(kind))
            if parent and parent.casefold() not in available:
                issues.append({
                    "row_number": number, "column_name": columns.get("parent_code", ""),
                    "severity": "error", "code": "cross_reference_missing",
                    "message": f"Parent org unit '{parent}' must appear before this row.",
                })
            available.add(code.casefold())
    elif dataset_type == "positions":
        available = {code.casefold() for code in Position.objects.values_list("code", flat=True)}
        for number, row in enumerate(import_batch.preview_rows or [], start=1):
            code = value(row, "code")
            manager = value(row, "reports_to_code")
            if manager and manager.casefold() not in available:
                issues.append({
                    "row_number": number, "column_name": columns.get("reports_to_code", ""),
                    "severity": "error", "code": "cross_reference_missing",
                    "message": f"Reports-to position '{manager}' must appear before this row.",
                })
            available.add(code.casefold())
    elif dataset_type == "matrix_reports":
        for number, row in enumerate(import_batch.preview_rows or [], start=1):
            pair = (
                value(row, "position_code").casefold(),
                value(row, "reports_to_code").casefold(),
            )
            if pair in seen:
                issues.append({
                    "row_number": number,
                    "column_name": columns.get("reports_to_code", ""),
                    "severity": "error", "code": "duplicate_record",
                    "message": "This matrix reporting pair appears more than once.",
                })
            seen.add(pair)
    return issues
