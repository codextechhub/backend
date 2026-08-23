"""Concurrency-safe tenant-level allocation for human-facing references."""
from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone


@transaction.atomic
def next_tenant_document_number(
    *, tenant, document_code: str, allocation_date: date | None = None,
) -> str:
    """Return ``<CODE>-<tenant_id><YYMMDD><n>`` for the requested local date.

    The unpadded sequence starts at one for every
    ``(tenant, document_code, local date)`` scope. The sequence row is protected
    by both a database uniqueness constraint and a row lock.
    """
    from .models import TenantDocumentSequence

    if tenant is None or tenant.pk is None:
        raise ValueError("Cannot allocate a document number without a saved tenant.")
    code = (document_code or "").strip().upper()
    if not code:
        raise ValueError("A document code is required.")
    day = allocation_date or timezone.localdate()

    TenantDocumentSequence.objects.get_or_create(
        tenant=tenant, document_code=code, date=day,
        defaults={"last_number": 0},
    )
    sequence = (
        TenantDocumentSequence.objects.select_for_update()
        .get(tenant=tenant, document_code=code, date=day)
    )
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    return f"{code}-{tenant.pk}{day:%y%m%d}{sequence.last_number}"
