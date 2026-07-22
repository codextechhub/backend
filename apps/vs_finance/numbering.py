"""Finance adapter for tenant-level human-facing document numbering."""
from __future__ import annotations

from datetime import date

from .exceptions import DocumentNumberingError


def next_document_number(
    *, entity, doc_type: str, allocation_date: date | None = None,
) -> str:
    """Allocate ``<DOC_TYPE>-<tenant_id><YYMMDD><n>`` for an entity's tenant.

    Branches and ledger entities do not create separate series. All entities
    owned by a tenant share the daily series for each document code.
    """
    from vs_tenants.numbering import next_tenant_document_number

    if entity is None:
        raise DocumentNumberingError("Cannot allocate a document number without an entity.")
    try:
        return next_tenant_document_number(
            tenant=entity.tenant,
            document_code=doc_type,
            allocation_date=allocation_date,
        )
    except ValueError as exc:
        raise DocumentNumberingError(str(exc)) from exc
