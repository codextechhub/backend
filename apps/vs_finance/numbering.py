"""Concurrency-safe, gap-free document numbering.

Produces identifiers scoped to an accounting **entity** (a set of books):

    CFX-LEKKI-INV-2026-00821        (entity LEKKI, no branch)
    CFX-LEKKI-B01-INV-2026-00821    (entity LEKKI, branch 1)
    CFX-CODEX-PAY-2026-00007        (Codex's own platform books)

Segments:
    CFX    - platform prefix (Code X Finance)
    LEKKI  - entity code (the set of books this document belongs to)
    B01    - optional branch token (zero-padded branch code); omitted when no branch
    INV    - document-type token (see :class:`~vs_finance.constants.DocType`)
    2026   - fiscal year
    00821  - zero-padded sequence, gap-free within the scope

The counter lives in :class:`~vs_finance.models.DocumentSequence`. Allocation locks
that row with ``select_for_update`` so concurrent callers serialise and can never be
handed the same number — the single most common source of duplicate-number bugs.
"""
from __future__ import annotations

from django.db import transaction

from .constants import DOC_NUMBER_PREFIX
from .exceptions import DocumentNumberingError

#: Width of the zero-padded sequence segment (00001 … 99999, then it simply grows).
SEQ_WIDTH = 5


def _branch_token(branch) -> str | None:
    """Render the optional branch segment: ``B07`` for branch code 7, else ``None``."""
    if branch is None:
        return None
    code = getattr(branch, "code", None)
    if not code:
        return None
    return f"B{int(code):02d}"


@transaction.atomic
def next_document_number(*, entity, branch, doc_type: str, fiscal_year: int) -> str:
    """Allocate the next document number for a scope and return the formatted string.

    Locks (or creates) the ``DocumentSequence`` row for
    ``(entity, branch, doc_type, fiscal_year)``, increments it, and formats the
    identifier. Runs in its own atomic block; the row lock is held only for the
    brief increment.

    Raises:
        DocumentNumberingError: if ``entity`` is missing.
    """
    from .models import DocumentSequence  # local import avoids import cycle at app load

    if entity is None:
        raise DocumentNumberingError("Cannot allocate a document number without an entity.")

    # Ensure the row exists, then re-fetch it under a row lock for the increment.
    DocumentSequence.objects.get_or_create(
        entity=entity, branch=branch, doc_type=doc_type, fiscal_year=fiscal_year,
        defaults={"last_number": 0},
    )
    seq = (
        DocumentSequence.objects
        .select_for_update()
        .get(entity=entity, branch=branch, doc_type=doc_type, fiscal_year=fiscal_year)
    )
    seq.last_number += 1
    seq.save(update_fields=["last_number", "updated_at"])

    parts = [DOC_NUMBER_PREFIX, entity.code]
    branch_token = _branch_token(branch)
    if branch_token:
        parts.append(branch_token)
    parts.extend([doc_type, str(fiscal_year), f"{seq.last_number:0{SEQ_WIDTH}d}"])
    return "-".join(parts)
