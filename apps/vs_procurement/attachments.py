"""Supporting-evidence files on vendor bills and vendor payments.

Two documents at opposite ends of the AP chain need the same thing: somewhere to keep
the counterparty's own paper. The supplier's invoice belongs against the bill we raised
from it; the receipt they issue belongs against the payment that triggered it. Both are
evidence rather than accounting - nothing here touches matching, allocation, or the GL.

One service backs both so the rules cannot drift apart. Deliberately, neither refuses on
document status: a supplier's formal invoice often follows the booked charge, and a
receipt *always* follows the payment, so gating uploads on DRAFT would reject exactly
the documents worth keeping.

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

from .models import VendorInvoiceAttachment, VendorPaymentAttachment

#: Enough for a bill, a delivery note, and a couple of photos, while keeping any one
#: document's storage footprint bounded - these rows hold their bytes in the database.
MAX_ATTACHMENTS_PER_DOCUMENT = 10


def _serialize(row) -> dict:
    """The shape both documents' detail payloads expose for one attachment."""
    return {
        "id": row.id,
        "name": row.original_name,
        "content_type": row.content_type,
        "size": row.size,
        "caption": row.caption,
        "url": signed_url(row.file.name),
        "uploaded_by_name": _uploader_name(row.uploaded_by),
        "uploaded_at": row.created_at,
    }


def _uploader_name(user) -> str:
    if user is None:
        return "System"
    full = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return full or getattr(user, "email", "System")


def serialize_attachments(document) -> list[dict]:
    """Serialize a document's attachments from its prefetched relation."""
    return [_serialize(row) for row in document.attachments.all()]


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
    if model.objects.filter(**{field: document}).count() >= MAX_ATTACHMENTS_PER_DOCUMENT:
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
    return _serialize(row)


@transaction.atomic
def remove_attachment(document, attachment_id) -> None:
    """Delete one attachment, but only from the document the caller already resolved.

    Filtering through ``document`` rather than by primary key alone is what keeps this
    inside the entity/branch isolation the calling view established - a bare
    ``pk=attachment_id`` would let any holder of the verb delete another tenant's file.
    """
    model, field = _model_and_field(document)
    row = model.objects.filter(**{field: document}, pk=attachment_id).first()
    if row is None:
        raise NotFound("No such attachment on this document.")
    row.file.delete(save=False)  # Drop the stored bytes, not just the pointer.
    row.delete()
