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


def _import_issue(
    *, row_number: int, column_name: str, code: str, message: str, raw_value="",
) -> dict:
    """Build one row-level issue in the import engine's public shape."""
    return {
        "row_number": row_number,
        "column_name": column_name,
        "severity": "error",
        "code": code,
        "message": message,
        "raw_value": raw_value,
    }


def _contains_cycle(edges: dict[str, str | None], start: str) -> bool:
    """Return whether following ``start`` through a parent map repeats a node."""
    visited = set()
    current = start
    while current:
        if current in visited:
            return True
        visited.add(current)
        current = edges.get(current)
    return False


def _is_import_boolean(value: str) -> bool:
    """Return whether a non-empty value uses the import engine's boolean syntax."""
    return not value or value.casefold() in {"true", "false", "yes", "no", "1", "0"}


def validate_organogram_import(import_batch) -> list[dict]:
    """Validate every deterministic organogram rule before execution.

    Rows execute from top to bottom, so references may resolve from the database
    or an earlier row, never a later one. The in-memory maps below follow that
    same order and include earlier updates, which makes cycle and uniqueness
    checks agree with what each row will encounter when it is written.
    """
    dataset_type = import_batch.template.dataset_type
    columns = {
        column.target_field: column.column_name
        for column in import_batch.template.columns.all()
    }

    def value(row, field):
        return str(row.get(columns.get(field, field)) or "").strip()

    def column(field):
        return columns.get(field, field)

    issues = []
    rows = import_batch.preview_rows or []
    if dataset_type == "org_units":
        node_state = {
            node.code.casefold(): {
                "kind": node.kind,
                "parent": node.parent.code.casefold() if node.parent_id else None,
                "name": node.name,
            }
            for node in OrgNode.objects.select_related("parent").all()
        }
        available = {key: state["kind"] for key, state in node_state.items()}
        parent_edges = {
            key: state["parent"] for key, state in node_state.items()
        }
        sibling_names = {
            (state["parent"], state["name"]): key
            for key, state in node_state.items()
        }
        seen_codes = set()

        for number, row in enumerate(rows, start=1):
            issue_count = len(issues)
            kind = value(row, "kind").upper()
            raw_code = value(row, "code")
            raw_parent = value(row, "parent_code")
            name = value(row, "name")
            if not raw_code or kind not in OrgNode.Kind.values:
                continue

            row_shape_valid = (
                bool(name)
                and len(raw_code) <= 40
                and len(name) <= 150
                and len(raw_parent) <= 40
                and _is_import_boolean(value(row, "is_active"))
            )

            normalized_code = _org_code(raw_code, kind)
            code_key = normalized_code.casefold()
            required_parent_kind = OrgNode._REQUIRED_PARENT_KIND[kind]
            normalized_parent = (
                _org_code(raw_parent, required_parent_kind) if raw_parent else ""
            )
            parent_key = normalized_parent.casefold() or None

            if code_key in seen_codes:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("code"),
                    code="duplicate_record",
                    message=(
                        f"Code '{raw_code}' resolves to '{normalized_code}', which "
                        "another row in this file already uses."
                    ),
                    raw_value=raw_code,
                ))
            seen_codes.add(code_key)

            parent_resolved = True
            if required_parent_kind is None and raw_parent:
                parent_resolved = False
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("parent_code"),
                    code="business_rule",
                    message="A Division is top-level and cannot have a parent.",
                    raw_value=raw_parent,
                ))
            elif required_parent_kind is not None and not raw_parent:
                parent_resolved = False
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("parent_code"),
                    code="required_value_missing",
                    message=(
                        f"Parent Code is required for a {OrgNode.Kind(kind).label}."
                    ),
                ))
            elif parent_key and parent_key not in available:
                parent_resolved = False
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("parent_code"),
                    code="cross_reference_missing",
                    message=(
                        f"Parent org unit '{normalized_parent}' must exist or "
                        "appear before this row."
                    ),
                    raw_value=raw_parent,
                ))
            elif parent_key and available[parent_key] != required_parent_kind:
                parent_resolved = False
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("parent_code"),
                    code="business_rule",
                    message=(
                        f"A {OrgNode.Kind(kind).label} must sit under a "
                        f"{OrgNode.Kind(required_parent_kind).label}."
                    ),
                    raw_value=raw_parent,
                ))

            if parent_resolved and name:
                name_owner = sibling_names.get((parent_key, name))
                if name and name_owner is not None and name_owner != code_key:
                    issues.append(_import_issue(
                        row_number=number,
                        column_name=column("name"),
                        code="duplicate_record",
                        message=(
                            f"An org unit named '{name}' already exists "
                            f"{'under this parent' if parent_key else 'at the top level'}."
                        ),
                        raw_value=name,
                    ))

                previous_edge = parent_edges.get(code_key)
                had_previous_edge = code_key in parent_edges
                parent_edges[code_key] = parent_key
                if _contains_cycle(parent_edges, code_key):
                    issues.append(_import_issue(
                        row_number=number,
                        column_name=column("parent_code"),
                        code="business_rule",
                        message="Org unit parent chain cannot contain a cycle.",
                        raw_value=raw_parent,
                    ))
                if had_previous_edge:
                    parent_edges[code_key] = previous_edge
                else:
                    parent_edges.pop(code_key, None)

            if len(issues) == issue_count and row_shape_valid:
                previous = node_state.get(code_key)
                if previous:
                    sibling_names.pop(
                        (previous["parent"], previous["name"]), None,
                    )
                sibling_names[(parent_key, name)] = code_key
                parent_edges[code_key] = parent_key
                node_state[code_key] = {
                    "kind": kind,
                    "parent": parent_key,
                    "name": name,
                }
                available[code_key] = kind
    elif dataset_type == "positions":
        org_units = {
            code.casefold() for code in OrgNode.objects.values_list("code", flat=True)
        }
        stored_positions = list(Position.objects.select_related("reports_to").all())
        available = {position.code.casefold() for position in stored_positions}
        reporting_edges = {
            position.code.casefold(): (
                position.reports_to.code.casefold()
                if position.reports_to_id else None
            )
            for position in stored_positions
        }
        role_keys = set()
        tenant = getattr(import_batch, "tenant", None)
        if tenant is not None:
            from vs_rbac.models import TenantRoleTemplate

            role_keys = {
                key
                for key in TenantRoleTemplate.objects.filter(
                    tenant=tenant,
                ).values_list("key", flat=True)
            }
        seen_codes = set()

        for number, row in enumerate(rows, start=1):
            issue_count = len(issues)
            raw_code = value(row, "code")
            if not raw_code:
                continue
            code_key = raw_code.casefold()
            title = value(row, "title")
            unit_code = value(row, "org_unit_code")
            manager_code = value(row, "reports_to_code")
            manager_key = manager_code.casefold() or None
            role_key = value(row, "default_role_key")
            raw_headcount = value(row, "headcount") or "1"
            row_shape_valid = (
                bool(title and unit_code)
                and len(raw_code) <= 40
                and len(title) <= 150
                and len(unit_code) <= 40
                and len(manager_code) <= 40
                and len(role_key) <= 120
                and _is_import_boolean(value(row, "is_active"))
            )

            if code_key in seen_codes:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("code"),
                    code="duplicate_record",
                    message=f"Position code '{raw_code}' appears more than once in this file.",
                    raw_value=raw_code,
                ))
            seen_codes.add(code_key)

            if unit_code and unit_code.casefold() not in org_units:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("org_unit_code"),
                    code="cross_reference_missing",
                    message=f"Org unit '{unit_code}' does not exist.",
                    raw_value=unit_code,
                ))

            manager_resolved = True
            if manager_key == code_key:
                manager_resolved = False
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("reports_to_code"),
                    code="business_rule",
                    message="A position cannot report to itself.",
                    raw_value=manager_code,
                ))
            elif manager_key and manager_key not in available:
                manager_resolved = False
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("reports_to_code"),
                    code="cross_reference_missing",
                    message=(
                        f"Reports-to position '{manager_code}' must exist or "
                        "appear before this row."
                    ),
                    raw_value=manager_code,
                ))

            if role_key and role_key not in role_keys:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("default_role_key"),
                    code="cross_reference_missing",
                    message=(
                        f"Role template '{role_key}' does not exist in the platform tenant."
                    ),
                    raw_value=role_key,
                ))

            try:
                headcount = int(raw_headcount)
            except (TypeError, ValueError):
                headcount = None
                row_shape_valid = False
            if headcount is not None and not 1 <= headcount <= 32767:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("headcount"),
                    code="business_rule",
                    message="Headcount must be between 1 and 32767.",
                    raw_value=raw_headcount,
                ))

            if manager_resolved:
                previous_edge = reporting_edges.get(code_key)
                had_previous_edge = code_key in reporting_edges
                reporting_edges[code_key] = manager_key
                if _contains_cycle(reporting_edges, code_key):
                    issues.append(_import_issue(
                        row_number=number,
                        column_name=column("reports_to_code"),
                        code="business_rule",
                        message="Position reporting chain cannot contain a cycle.",
                        raw_value=manager_code,
                    ))
                if had_previous_edge:
                    reporting_edges[code_key] = previous_edge
                else:
                    reporting_edges.pop(code_key, None)

            if len(issues) == issue_count and row_shape_valid:
                reporting_edges[code_key] = manager_key
                available.add(code_key)
    elif dataset_type == "matrix_reports":
        positions = {
            code.casefold() for code in Position.objects.values_list("code", flat=True)
        }
        seen = set()
        for number, row in enumerate(rows, start=1):
            position_code = value(row, "position_code")
            manager_code = value(row, "reports_to_code")
            pair = (
                position_code.casefold(),
                manager_code.casefold(),
            )
            if position_code and pair[0] not in positions:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("position_code"),
                    code="cross_reference_missing",
                    message=f"Position '{position_code}' does not exist.",
                    raw_value=position_code,
                ))
            if manager_code and pair[1] not in positions:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("reports_to_code"),
                    code="cross_reference_missing",
                    message=f"Reports-to position '{manager_code}' does not exist.",
                    raw_value=manager_code,
                ))
            if position_code and manager_code and pair[0] == pair[1]:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("reports_to_code"),
                    code="business_rule",
                    message="A position cannot have a matrix line to itself.",
                    raw_value=manager_code,
                ))
            if position_code and manager_code and pair in seen:
                issues.append(_import_issue(
                    row_number=number,
                    column_name=column("reports_to_code"),
                    code="duplicate_record",
                    message="This matrix reporting pair appears more than once.",
                    raw_value=manager_code,
                ))
            if position_code and manager_code:
                seen.add(pair)
    return issues
