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

One optional marker crosses it too. A field group item or a table column may
name the Field Access key its values come from, and :func:`for_reader` then
drops it for an approver whose roles may not read that field. The layout is
snapshotted when the document is submitted and read later by whoever approves
it, so the marker, rather than the value, is what the snapshot has to carry:
filtering at submission would answer for the submitter and hand every approver
the same answer.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
SECTION_KINDS = frozenset({"fields", "table", "changes"})
CHANGE_OPERATIONS = frozenset({"ADD", "REMOVE"})


class InvalidDocumentDetails(ValueError):
    """The handler returned a detail layout the generic renderer cannot trust."""


def _unpack(entry, size: int):
    """*entry*'s ``size`` parts, plus the optional Field Access key after them."""
    parts = tuple(entry)
    if len(parts) == size:
        return parts + (None,)
    if len(parts) == size + 1:
        return parts
    raise InvalidDocumentDetails(
        f"A presentation entry takes {size} parts, or {size + 1} with a "
        f"Field Access key; this one has {len(parts)}."
    )


def fields_section(title: str, items: Iterable[tuple]) -> dict:
    """Build a labelled field group and omit facts that have no value.

    An item is ``(label, value)``, or ``(label, value, access)`` where the
    value comes from a registered field and only an approver who may read that
    field should see it.
    """
    display_items = []
    for item in items:
        label, value, access = _unpack(item, 2)
        display_value = _display(value)
        if display_value.strip():
            row = {"label": str(label), "value": display_value}
            if access:
                row["access"] = str(access)
            display_items.append(row)
    return {
        "kind": "fields",
        "title": str(title),
        "items": display_items,
    }


def table_section(
    title: str,
    columns: Sequence[tuple],
    rows: Iterable[Mapping[str, object]],
) -> dict:
    """Build a generic table whose cells contain display values only.

    A column is ``(key, label)``, or ``(key, label, access)`` where the column
    holds a registered field and an approver who may not read that field
    should not be given the column at all.
    """
    column_rows = []
    for column in columns:
        key, label, access = _unpack(column, 2)
        row = {"key": str(key), "label": str(label)}
        if access:
            row["access"] = str(access)
        column_rows.append(row)
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
        if not isinstance(item, dict) or not {"label", "value"} <= set(item) <= {
            "label", "value", "access",
        }:
            raise InvalidDocumentDetails(f"{where} must contain label and value.")
        _required_text(item, "label", where)
        _text(item, "value", where)
        if "access" in item:
            _required_text(item, "access", where)
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
        if not isinstance(column, dict) or not {"key", "label"} <= set(column) <= {
            "key", "label", "access",
        }:
            raise InvalidDocumentDetails(f"{where} must contain key and label.")
        _required_text(column, "key", where)
        _required_text(column, "label", where)
        if "access" in column:
            _required_text(column, "access", where)
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


def for_reader(details: Mapping, request) -> dict:
    """*details* with everything the request's caller may not read removed.

    An approval document is snapshotted when it is submitted and read by
    whoever approves it, which may be days later and is rarely the person who
    built it. So the snapshot keeps the Field Access key beside each value it
    took from a registered field, and the filtering happens here, on the way
    out, for the person actually reading it.

    A field group loses the items they may not read; a table loses the whole
    column, because a column of blanks still says how many rows have a value.
    A section left with nothing is dropped rather than shown empty, and the
    snapshot itself is never rewritten.
    """
    from vs_rbac.field_enforcement import can_read

    if not details or not details.get("sections"):
        return dict(details or {})

    sections = []
    for section in details["sections"]:
        if section.get("kind") == "fields":
            items = [
                item for item in section["items"]
                if not item.get("access") or can_read(request, item["access"])
            ]
            if not items:
                continue
            sections.append({**section, "items": items})
        elif section.get("kind") == "table":
            columns = [
                column for column in section["columns"]
                if not column.get("access") or can_read(request, column["access"])
            ]
            if not columns:
                continue
            keys = {column["key"] for column in columns}
            sections.append({
                **section,
                "columns": columns,
                "rows": [
                    {key: value for key, value in row.items() if key in keys}
                    for row in section["rows"]
                ],
            })
        else:
            sections.append(section)
    return {**details, "sections": sections}


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
