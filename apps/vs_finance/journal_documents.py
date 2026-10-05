"""Name the document behind each of many journals at once.

A listing of ledger lines (a tax return's lines, say) shows, beside each line,
the invoice, bill or payroll run that raised its journal, so a bursar can tell
what was declared without opening every journal. Asking
:func:`vs_finance.posting._journal_document_owner` per line would cost a few
dozen queries per line; :func:`journal_documents` answers a whole page in a
handful.
"""
from __future__ import annotations

import re

#: Rows that raise a journal on behalf of a parent document, and the field that
#: names the parent. The parent is the document a person knows: a payroll run
#: rather than one branch's share of it, a transfer rather than one of its legs.
PARENT_DOCUMENTS = {
    "vs_finance.PayrollRunBranch": "run",
    "vs_finance.InterBranchTransferLeg": "transfer",
    "vs_finance.TaxFilingShare": "filing",
    "vs_finance.TaxRemittance": "filing",
    "vs_finance.DepreciationSchedule": "asset",
    "vs_finance.WriteOffRecovery": "write_off",
}


def document_type(model) -> str:
    """``VENDOR_INVOICE`` for ``VendorInvoice``: the type token the journal screen uses."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).upper()


def _owning_relations():
    """``(model, journal field, parent field or None)`` for every journal owner with a number.

    Read from model metadata in the order :func:`vs_finance.posting._journal_document_owner`
    walks it, so both name the same document when two could claim a journal. A
    row with no document number of its own counts only through a parent named
    in :data:`PARENT_DOCUMENTS`; a stock movement, which never outranks the
    receipt or return that valued it, is not an owner here.
    """
    from .models import JournalEntry

    relations = []
    for relation in JournalEntry._meta.related_objects:
        field = relation.field.name
        if "journal" not in field:
            continue
        model = relation.related_model
        if any(f.name == "document_number" for f in model._meta.concrete_fields):
            relations.append((model, field, None))
        elif model._meta.label in PARENT_DOCUMENTS:
            relations.append((model, field, PARENT_DOCUMENTS[model._meta.label]))
    return relations


def journal_documents(entry_ids) -> dict:
    """``{entry_id: {"type", "id", "number"}}``: the document behind each journal.

    A reversal is named by the document of the journal it reverses (a voided
    invoice's mirror journal is the invoice's). A journal no document claims (a
    hand-typed journal, a year-end closing entry) is named as itself, type
    ``JOURNAL`` with its own number. One query for the journals, then one per
    kind of owning document, stopping as soon as every journal is named: a page
    of a VAT return made of invoice journals costs three.
    """
    from .models import JournalEntry

    ids = {int(pk) for pk in entry_ids if pk is not None}
    if not ids:
        return {}
    entries = {
        pk: (reverses_id, number)
        for pk, reverses_id, number in JournalEntry.objects.filter(pk__in=ids)
        .values_list("pk", "reverses_id", "document_number")
    }
    owner_of = {pk: reverses_id or pk for pk, (reverses_id, _number) in entries.items()}
    pending = set(owner_of.values())
    named = {}
    for model, field, parent in _owning_relations():
        if not pending:
            break
        prefix = f"{parent}__" if parent else ""
        rows = (
            model._default_manager.filter(**{f"{field}_id__in": pending})
            .order_by("pk")
            .values_list(f"{field}_id", f"{prefix}pk", f"{prefix}document_number")
        )
        kind = document_type(model._meta.get_field(parent).related_model if parent else model)
        for journal_id, pk, number in rows:
            if journal_id in pending:
                named[journal_id] = {"type": kind, "id": pk, "number": number or str(pk)}
                pending.discard(journal_id)
    result = {}
    for pk, (_reverses_id, number) in entries.items():
        result[pk] = named.get(owner_of[pk]) or {"type": "JOURNAL", "id": pk, "number": number}
    return result
