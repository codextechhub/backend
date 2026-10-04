"""Supporting-evidence files on vendor bills and vendor payments.

Two documents at opposite ends of the AP chain need the same thing: somewhere to keep
the counterparty's own paper. The supplier's invoice belongs against the bill we raised
from it; the receipt they issue belongs against the payment that triggered it. Both are
evidence rather than accounting - nothing here touches matching, allocation, or the GL.

One service backs both so the rules cannot drift apart. Deliberately, neither refuses on
document status: a supplier's formal invoice often follows the booked charge, and a
receipt *always* follows the payment, so gating uploads on DRAFT would reject exactly
the documents worth keeping.

Removing a file depends on the document. On a draft the upload is somebody's
unfinished work and is deleted. Once the document has left draft the file is
evidence behind a record the law requires the tenant to keep, so removal
**supersedes** it instead: a reason is required, the row and its bytes stay, the
file drops out of the document's current list (``include_superseded`` shows it),
and the act is written to the finance audit trail with the actor. Adding a file
is audited too. :mod:`core.retention` refuses any hard delete of such a file that
does not come through here.

Files are stored through ``core.storage.DatabaseStorage`` and served through
``core.media``, which binds each one to its tenant and its owning document and
re-asks the permission question on every read. The URL handed out here is signed
for the caller and short-lived, so it stops working when they walk away from it.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from core.media import signed_url
from core.uploads import validate_upload

from vs_finance.audit import record
from vs_finance.close import require_reason
from vs_finance.constants import DocumentStatus, FinanceAuditAction

from .models import VendorInvoiceAttachment, VendorPaymentAttachment

#: Enough for a bill, a delivery note, and a couple of photos, while keeping any one
#: document's storage footprint bounded - these rows hold their bytes in the database.
MAX_ATTACHMENTS_PER_DOCUMENT = 10


def _serialize(row, states) -> dict:
    """The shape both documents' detail payloads expose for one attachment."""
    return {
        "id": row.id,
        "name": row.original_name,
        "content_type": row.content_type,
        "size": row.size,
        "caption": row.caption,
        "url": signed_url(row.file.name),
        "uploaded_by_name": _uploader_name(row.uploaded_by),
        "uploaded_by_is_exited": states.get(row.uploaded_by_id) if row.uploaded_by_id else None,
        "uploaded_at": row.created_at,
        "superseded": row.superseded_at is not None,
        "superseded_at": row.superseded_at,
        "superseded_by_name": _uploader_name(row.superseded_by) if row.superseded_at else None,
        "superseded_by_is_exited": (
            states.get(row.superseded_by_id) if row.superseded_by_id else None
        ),
        "superseded_reason": row.superseded_reason,
    }


def _uploader_name(user) -> str:
    if user is None:
        return "System"
    full = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return full or getattr(user, "email", "System")


def serialize_attachments(document, *, include_superseded=False) -> list[dict]:
    """Serialize a document's current attachments from its prefetched relation.

    Superseded files are left out unless ``include_superseded``.
    """
    from core.person_exit import prime_exit_states

    rows = [
        row for row in document.attachments.all()
        if include_superseded or row.superseded_at is None
    ]
    states = prime_exit_states({}, (
        user_id for row in rows
        for user_id in (row.uploaded_by_id, row.superseded_by_id)
    ))
    return [_serialize(row, states) for row in rows]


def _audit(document, action, row, message, **metadata):
    record(
        entity=document.entity, action=action, target=document,
        message=message, attachment_id=row.pk, file_name=row.original_name,
        file_size=row.size, **metadata,
    )


def _model_and_field(document):
    """Map the owning document to its attachment model and foreign-key name."""
    from .models import VendorInvoice, VendorPayment

    if isinstance(document, VendorInvoice):
        return VendorInvoiceAttachment, "vendor_invoice"
    if isinstance(document, VendorPayment):
        return VendorPaymentAttachment, "payment"
    raise TypeError(f"{type(document).__name__} does not carry attachments.")


@transaction.atomic
def add_attachment(document, upload, *, caption="", actor_user=None) -> dict:
    """Validate and store one file against ``document``; return its serialized row."""
    model, field = _model_and_field(document)
    # Lock the owning document so two concurrent uploads cannot both read a count of
    # nine and both write, leaving eleven rows behind the cap.
    type(document).objects.select_for_update().filter(pk=document.pk).first()
    current = model.objects.filter(**{field: document}, superseded_at__isnull=True)
    if current.count() >= MAX_ATTACHMENTS_PER_DOCUMENT:
        raise ValidationError({
            "file": f"This document already has {MAX_ATTACHMENTS_PER_DOCUMENT} "
                    f"attachments, the maximum. Remove one before adding another.",
        })
    name, content_type = validate_upload(upload)
    row = model.objects.create(
        **{field: document},
        file=upload,
        original_name=name,
        content_type=content_type,
        size=upload.size,
        caption=str(caption or "").strip()[:255],
        uploaded_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
    )
    _audit(
        document, FinanceAuditAction.ATTACHMENT_ADDED, row,
        f"Attached '{row.original_name}'.", actor_user=actor_user,
    )
    from core.person_exit import prime_exit_states

    states = prime_exit_states({}, (row.uploaded_by_id,))
    return _serialize(row, states)


@transaction.atomic
def remove_attachment(document, attachment_id, *, reason=None, actor_user=None) -> None:
    """Take one attachment off ``document``: delete it from a draft, supersede it otherwise.

    Filtering through ``document`` rather than by primary key alone is what keeps this
    inside the entity/branch isolation the calling view established - a bare
    ``pk=attachment_id`` would let any holder of the verb delete another tenant's file.

    On a document that has left draft a ``reason`` is required: the file is marked
    superseded with the actor, the time and the reason, and nothing is deleted.
    """
    from django.utils import timezone

    model, field = _model_and_field(document)
    row = model.objects.filter(
        **{field: document}, pk=attachment_id, superseded_at__isnull=True,
    ).first()
    if row is None:
        raise NotFound("No such attachment on this document.")
    if document.status == DocumentStatus.DRAFT:
        _audit(
            document, FinanceAuditAction.ATTACHMENT_REMOVED, row,
            f"Removed '{row.original_name}' from a draft.", actor_user=actor_user,
        )
        row.file.delete(save=False)  # A draft's upload is unfinished work; its bytes go.
        row.delete()
        return
    reason = require_reason(reason, act="remove evidence from a document that has left draft")
    row.superseded_at = timezone.now()
    row.superseded_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
    row.superseded_reason = reason
    row.save(update_fields=["superseded_at", "superseded_by", "superseded_reason", "updated_at"])
    _audit(
        document, FinanceAuditAction.ATTACHMENT_SUPERSEDED, row,
        f"Superseded '{row.original_name}': {reason}"[:255], actor_user=actor_user,
        reason=reason,
    )
