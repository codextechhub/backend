"""Field Access for the rows of a spreadsheet import.

An import runs as a person, not inside a request, and writes through services
that never meet the form's guard. Every dataset that writes a registered
resource therefore asks the same questions of each non-blank cell, here, so a
preview and the execution that follows cannot disagree:

* a field the runner may not write is refused, unless the registry declares it
  open on create, which belongs to the act of creating the record;
* a blank cell sets nothing, exactly as a form's untouched input is dropped,
  so it is never refused;
* the refusal names the template's target fields, the keys a preview maps back
  to columns, in the registry's labels.

The preview reports a refusal as an error against the row and column, with
``metadata.field_write_denied`` listing the target field. Execution raises
:class:`~vs_rbac.field_enforcement.FieldWriteDenied` for the row, which the
engine records as a failed row with the same sentence and
``error_details.field_write_denied``.
"""
from __future__ import annotations

from types import SimpleNamespace

from vs_rbac.field_enforcement import FieldWriteDenied, assert_writable


def import_request(actor, tenant, holder=None):
    """The request-shaped stand-in an import's Field Access questions are asked of.

    Field Access judges ``request.user`` in ``request.tenant``. The tenant is
    the one the rows are written to. Passing the batch as *holder* keeps the
    stand-in on it, so a file of a thousand rows evaluates the actor's roles
    once. ``None`` for no *actor* is the escape hatch Field Access names: a
    preview made for nobody refuses nothing.
    """
    if actor is None:
        return None
    cached = getattr(holder, "_field_access_request", None)
    if cached is not None and cached.user is actor and cached.tenant is tenant:
        return cached
    request = SimpleNamespace(user=actor, tenant=tenant)
    if holder is not None:
        holder._field_access_request = request
    return request


def _non_blank_names(target: str, names: tuple[str, ...], payload: dict) -> tuple[str, ...]:
    """Every registered name of *target* when its cell is not blank, none when it is."""
    value = payload.get(target)
    if value is None or not str(value).strip():
        return ()
    return tuple(names)


def write_refusal(request, payload: dict, writes, *, names_set=_non_blank_names) -> FieldWriteDenied | None:
    """The refusal that setting the non-blank cells of *payload* as *request*'s caller would meet.

    *writes* is a list of ``(resource, {target field: registered names},
    label prefix)``. The prefix goes before a label that would otherwise be
    ambiguous between two resources written from one row. ``None`` request
    refuses nothing.

    ``names_set(target, names, payload)`` answers which of a target's
    registered names its cell sets. By default all of them when the cell is
    not blank; a dataset whose one column fills several fields only in part (a
    guardian's full name that has no middle name) passes its own.
    """
    if request is None:
        return None
    denied: dict[str, str] = {}
    for resource, mapping, prefix in writes:
        owners: dict[str, list[str]] = {}
        for target, names in mapping.items():
            for name in names_set(target, names, payload):
                owners.setdefault(name, []).append(target)
        if not owners:
            continue
        try:
            assert_writable(request, resource, dict.fromkeys(owners, True), creating=True)
        except FieldWriteDenied as refusal:
            for name in refusal.fields:
                for target in owners[name]:
                    denied.setdefault(target, f"{prefix}{refusal.labels[name]}")
    if not denied:
        return None
    return FieldWriteDenied(denied, labels=denied)


def write_refusal_issue(refusal, field: str, row_number: int, header: dict) -> dict:
    """One refused field of one row, in the engine's issue shape.

    Worded by the refusal execution raises, so a preview tells an operator whose
    write switch is closed what the import will tell them.
    """
    return {
        "severity": "error",
        "code": "business_rule",
        "message": FieldWriteDenied([field], labels=refusal.labels).message,
        "row_number": row_number,
        "column_name": header.get(field, field),
        "raw_value": "",
        "metadata": {"field_write_denied": [field]},
    }
