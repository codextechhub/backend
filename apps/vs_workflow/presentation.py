"""Validated presentation primitives for approval document details.

The workflow engine stores what an approver needs to inspect, but it does not
know the shape of a journal, leave request, permission change, or purchase
order. Handlers therefore describe those documents with a deliberately small
set of semantic blocks. The frontend owns their visual treatment; handlers own
which business facts are safe and necessary to disclose.

Only display strings and explicit booleans cross this boundary. Raw model
serializers, metadata dictionaries, HTML, and model attribute paths are not
part of the contract, so a new handler cannot accidentally turn the approval
surface into an unrestricted document export.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
SECTION_KINDS = frozenset({"fields", "table", "changes"})
CHANGE_OPERATIONS = frozenset({"ADD", "REMOVE"})


class InvalidDocumentDetails(ValueError):
    """The handler returned a detail layout the generic renderer cannot trust."""


def fields_section(title: str, items: Iterable[tuple[str, object]]) -> dict:
    """Build a labelled field group and omit facts that have no value."""
    display_items = []
    for label, value in items:
        display_value = _display(value)
        if display_value.strip():
            display_items.append({"label": str(label), "value": display_value})
    return {
        "kind": "fields",
        "title": str(title),
        "items": display_items,
    }


def table_section(
    title: str,
    columns: Sequence[tuple[str, str]],
    rows: Iterable[Mapping[str, object]],
) -> dict:
    """Build a generic table whose cells contain display values only."""
    column_rows = [{"key": str(key), "label": str(label)} for key, label in columns]
    keys = [column["key"] for column in column_rows]
    return {
        "kind": "table",
        "title": str(title),
        "columns": column_rows,
        "rows": [
            {key: _display(row.get(key, "")) for key in keys}
            for row in rows
        ],
    }


def changes_section(title: str, items: Iterable[Mapping[str, object]]) -> dict:
    """Build an added/removed list with semantic risk markers."""
    rows = []
    for item in items:
        row = {
            "operation": str(item.get("operation", "")),
            "label": _display(item.get("label", "")),
            "restricted": bool(item.get("restricted", False)),
        }
        description = _display(item.get("description", ""))
        if description:
            row["description"] = description
        rows.append(row)
    return {"kind": "changes", "title": str(title), "items": rows}


def document_details(*sections: dict) -> dict:
    """Build and validate one versioned approval-detail snapshot."""
    payload = {"schema_version": SCHEMA_VERSION, "sections": list(sections)}
    return validate_document_details(payload)


def validate_document_details(payload) -> dict:
    """Return a safe copy of a handler layout or raise a precise contract error.

    An empty dictionary means that this document type has no embedded layout.
    It is the backward-compatible value for instances created before details
    existed and for handlers that have not opted in.
    """
    if payload in (None, {}):
        return {}
    if not isinstance(payload, dict):
        raise InvalidDocumentDetails("Document details must be an object.")
    if set(payload) != {"schema_version", "sections"}:
        raise InvalidDocumentDetails(
            "Document details may contain only schema_version and sections."
        )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise InvalidDocumentDetails(
            f"Document details schema_version must be {SCHEMA_VERSION}."
        )
    sections = payload["sections"]
    if not isinstance(sections, list):
        raise InvalidDocumentDetails("Document detail sections must be a list.")

    validated = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise InvalidDocumentDetails(f"Section {index + 1} must be an object.")
        kind = section.get("kind")
        if kind not in SECTION_KINDS:
            raise InvalidDocumentDetails(
                f"Section {index + 1} has unsupported kind {kind!r}."
            )
        _required_text(section, "title", f"Section {index + 1}")
        if kind == "fields":
            validated.append(_validate_fields(section, index))
        elif kind == "table":
            validated.append(_validate_table(section, index))
        else:
            validated.append(_validate_changes(section, index))
    return {"schema_version": SCHEMA_VERSION, "sections": validated}


def _validate_fields(section: dict, index: int) -> dict:
    if set(section) != {"kind", "title", "items"}:
        raise InvalidDocumentDetails(
            f"Fields section {index + 1} contains unsupported properties."
        )
    items = section.get("items")
    if not isinstance(items, list):
        raise InvalidDocumentDetails(f"Fields section {index + 1} items must be a list.")
    clean = []
    for item_index, item in enumerate(items):
        where = f"Fields section {index + 1}, item {item_index + 1}"
        if not isinstance(item, dict) or set(item) != {"label", "value"}:
            raise InvalidDocumentDetails(f"{where} must contain label and value.")
        _required_text(item, "label", where)
        _text(item, "value", where)
        clean.append(dict(item))
    return {"kind": "fields", "title": section["title"], "items": clean}


def _validate_table(section: dict, index: int) -> dict:
    if set(section) != {"kind", "title", "columns", "rows"}:
        raise InvalidDocumentDetails(
            f"Table section {index + 1} contains unsupported properties."
        )
    columns = section.get("columns")
    rows = section.get("rows")
    if not isinstance(columns, list) or not columns:
        raise InvalidDocumentDetails(f"Table section {index + 1} needs columns.")
    if not isinstance(rows, list):
        raise InvalidDocumentDetails(f"Table section {index + 1} rows must be a list.")
    clean_columns = []
    keys = []
    for column_index, column in enumerate(columns):
        where = f"Table section {index + 1}, column {column_index + 1}"
        if not isinstance(column, dict) or set(column) != {"key", "label"}:
            raise InvalidDocumentDetails(f"{where} must contain key and label.")
        _required_text(column, "key", where)
        _required_text(column, "label", where)
        if column["key"] in keys:
            raise InvalidDocumentDetails(f"{where} repeats key {column['key']!r}.")
        keys.append(column["key"])
        clean_columns.append(dict(column))
    clean_rows = []
    for row_index, row in enumerate(rows):
        where = f"Table section {index + 1}, row {row_index + 1}"
        if not isinstance(row, dict) or set(row) != set(keys):
            raise InvalidDocumentDetails(f"{where} must contain exactly the column keys.")
        for key in keys:
            _text(row, key, where)
        clean_rows.append(dict(row))
    return {
        "kind": "table",
        "title": section["title"],
        "columns": clean_columns,
        "rows": clean_rows,
    }


def _validate_changes(section: dict, index: int) -> dict:
    if set(section) != {"kind", "title", "items"}:
        raise InvalidDocumentDetails(
            f"Changes section {index + 1} contains unsupported properties."
        )
    items = section.get("items")
    if not isinstance(items, list):
        raise InvalidDocumentDetails(f"Changes section {index + 1} items must be a list.")
    clean = []
    for item_index, item in enumerate(items):
        where = f"Changes section {index + 1}, item {item_index + 1}"
        if not isinstance(item, dict):
            raise InvalidDocumentDetails(f"{where} must be an object.")
        if not set(item).issubset({"operation", "label", "description", "restricted"}):
            raise InvalidDocumentDetails(f"{where} contains unsupported properties.")
        if set(item) < {"operation", "label", "restricted"}:
            raise InvalidDocumentDetails(
                f"{where} must contain operation, label, and restricted."
            )
        if item["operation"] not in CHANGE_OPERATIONS:
            raise InvalidDocumentDetails(f"{where} has an invalid operation.")
        _required_text(item, "label", where)
        if "description" in item:
            _text(item, "description", where)
        if not isinstance(item["restricted"], bool):
            raise InvalidDocumentDetails(f"{where} restricted must be true or false.")
        clean.append(dict(item))
    return {"kind": "changes", "title": section["title"], "items": clean}


def _display(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _required_text(row: dict, key: str, where: str) -> None:
    _text(row, key, where)
    if not row[key].strip():
        raise InvalidDocumentDetails(f"{where} {key} cannot be blank.")


def _text(row: dict, key: str, where: str) -> None:
    if not isinstance(row.get(key), str):
        raise InvalidDocumentDetails(f"{where} {key} must be text.")
