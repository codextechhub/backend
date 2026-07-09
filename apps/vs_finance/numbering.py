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
from __future__ import annotations  # Keep type annotations from importing at runtime.

from django.db import transaction  # Provides atomic row-locking for sequence increments.

from .constants import DOC_NUMBER_PREFIX  # Platform-wide document number prefix.
from .exceptions import DocumentNumberingError  # Raised when numbering cannot proceed safely.

#: Width of the zero-padded sequence segment (00001 … 99999, then it simply grows).
SEQ_WIDTH = 5  # Minimum width for the numeric suffix.


def _branch_token(branch) -> str | None:  # Build the optional branch segment for a document number.
    """Render the optional branch segment: ``B07`` for branch code 7, else ``None``."""
    if branch is None:  # Entity-level documents omit the branch segment.
        return None
    code = getattr(branch, "code", None)  # Read branch code defensively from the object.
    if not code:  # Branches without a code cannot contribute a token.
        return None
    return f"B{int(code):02d}"  # Render a zero-padded numeric branch token.


@transaction.atomic
def next_document_number(*, entity, branch, doc_type: str, fiscal_year: int) -> str:  # Allocate one scoped document number.
    """Allocate the next document number for a scope and return the formatted string.

    Locks (or creates) the ``DocumentSequence`` row for
    ``(entity, branch, doc_type, fiscal_year)``, increments it, and formats the
    identifier. Runs in its own atomic block; the row lock is held only for the
    brief increment.

    Raises:
        DocumentNumberingError: if ``entity`` is missing.
    """
    from .models import DocumentSequence  # Local import avoids import cycle at app load.

    if entity is None:  # A document number must always belong to a ledger entity.
        raise DocumentNumberingError("Cannot allocate a document number without an entity.")

    # Ensure the row exists, then re-fetch it under a row lock for the increment.  # Avoid duplicate sequence rows.
    DocumentSequence.objects.get_or_create(  # Create the sequence scope on first use.
        entity=entity, branch=branch, doc_type=doc_type, fiscal_year=fiscal_year,  # Scope sequence by books, branch, type, and year.
        defaults={"last_number": 0},  # Start before the first allocated number.
    )
    seq = (  # Reload the same sequence while holding a database row lock.
        DocumentSequence.objects  # Start from the sequence manager.
        .select_for_update()  # Serialize concurrent allocators for this exact scope.
        .get(entity=entity, branch=branch, doc_type=doc_type, fiscal_year=fiscal_year)  # Fetch the locked row.
    )
    seq.last_number += 1  # Advance to the next available sequence value.
    seq.save(update_fields=["last_number", "updated_at"])  # Persist only the changed counter fields.

    parts = [DOC_NUMBER_PREFIX, entity.code]  # Start with platform prefix and entity code.
    branch_token = _branch_token(branch)  # Compute the optional branch token.
    if branch_token:  # Branch-scoped sequences include branch in the document number.
        parts.append(branch_token)  # Insert branch token before document type.
    parts.extend([doc_type, str(fiscal_year), f"{seq.last_number:0{SEQ_WIDTH}d}"])  # Add type, year, and padded number.
    return "-".join(parts)  # Return the final human-readable document number.
