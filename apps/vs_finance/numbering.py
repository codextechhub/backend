"""Concurrency-safe, gap-free document numbering.

Produces identifiers scoped to an accounting **entity** (a set of books):

    LEK-IV-2600821        (entity LEKKI, no branch)
    LEK-B01-IV-2600821    (entity LEKKI, branch 1)
    COD-PY-2600007        (Codex's own platform books)

Segments:
    LEK      - entity number_code (the set of books this document belongs to)
    B01      - optional branch token (zero-padded branch code); omitted when no branch
    IV       - 2-char document-type token (see :class:`~vs_finance.constants.DocType`)
    2600821  - merged block: 2-digit fiscal year (26) + gap-free zero-padded sequence

The counter lives in :class:`~vs_finance.models.DocumentSequence`. Allocation locks
that row with ``select_for_update`` so concurrent callers serialise and can never be
handed the same number — the single most common source of duplicate-number bugs.
"""
from __future__ import annotations

from django.db import transaction

from .exceptions import DocumentNumberingError

#: Width of the zero-padded sequence segment (00001 … 99999, then it simply grows).
SEQ_WIDTH = 5  # Minimum width for the numeric suffix.


# Build the optional branch segment for a document number.
def _branch_token(branch) -> str | None:
    """Render the optional branch segment: ``B07`` for branch code 7, else ``None``."""
    if branch is None:  # Entity-level documents omit the branch segment.
        return None
    code = getattr(branch, "code", None)  # Read branch code defensively from the object.
    if not code:  # Branches without a code cannot contribute a token.
        return None
    return f"B{int(code):02d}"  # Render a zero-padded numeric branch token.


@transaction.atomic
# Allocate one scoped document number.
def next_document_number(*, entity, branch, doc_type: str, fiscal_year: int) -> str:
    """Allocate the next document number for a scope and return the formatted string.

    Locks (or creates) the ``DocumentSequence`` row for
    ``(entity, branch, doc_type, fiscal_year)``, increments it, and formats the
    identifier. Runs in its own atomic block; the row lock is held only for the
    brief increment.

    Raises:
        DocumentNumberingError: if ``entity`` is missing.
    """
    from .models import DocumentSequence

    if entity is None:  # A document number must always belong to a ledger entity.
        raise DocumentNumberingError("Cannot allocate a document number without an entity.")

    # Ensure the row exists, then re-fetch it under a row lock for the increment.  # Avoid duplicate sequence rows.
    DocumentSequence.objects.get_or_create(
        entity=entity, branch=branch, doc_type=doc_type, fiscal_year=fiscal_year,  # Scope sequence by books, branch, type, and year.
        defaults={"last_number": 0},  # Start before the first allocated number.
    )
    seq = (  # Reload the same sequence while holding a database row lock.
        DocumentSequence.objects
        .select_for_update()  # Serialize concurrent allocators for this exact scope.
        .get(entity=entity, branch=branch, doc_type=doc_type, fiscal_year=fiscal_year)
    )
    seq.last_number += 1  # Advance to the next available sequence value.
    seq.save(update_fields=["last_number", "updated_at"])

    # The entity's short number_code identifies the set of books (e.g. COD).
    parts = [entity.number_code or entity.code]
    branch_token = _branch_token(branch)  # Compute the optional branch token.
    if branch_token:  # Branch-scoped sequences include branch in the document number.
        parts.append(branch_token)  # Insert branch token before document type.
    # Year + sequence are one merged block: a 2-digit year prefix (26) glued to the
    # padded counter (00001) — no separator — so "26" stays fixed while the number
    # grows on its own (26100000 once it passes 100k).
    year_sequence = f"{fiscal_year % 100:02d}{seq.last_number:0{SEQ_WIDTH}d}"
    parts.extend([doc_type, year_sequence])  # Add the 2-char type token and the merged year+number.
    return "-".join(parts)  # Return the final human-readable document number.
