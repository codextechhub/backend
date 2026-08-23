"""Shared first-line validation for user-supplied file uploads.

``core.storage.DatabaseStorage`` already enforces an extension allowlist and a size
ceiling as defense-in-depth, but it raises Django's ``ValidationError`` from inside
``_save`` - which surfaces as an unhandled 500 rather than a 400. Every upload
endpoint therefore needs a first line of validation, and until this module existed
each one wrote its own: the vendor portal checked magic bytes, vs_tickets checked
extension and size only, and the expense-claim receipt endpoint checked nothing at
all (so an oversized or unsupported file 500'd from storage).

This is the choke point. Callers pass their own policy - the allowed extensions and
the size ceiling differ legitimately between a public vendor portal and an internal
ticket attachment - and get back the sanitised filename and the content type implied
by the extension, having proved the bytes match that extension.

The magic-byte check is defence in depth, not a live vulnerability fix. ``MediaView``
already sets ``X-Content-Type-Options: nosniff`` and forces
``Content-Disposition: attachment`` on everything that is not ``image/*``, so an HTML
payload renamed ``.png`` is served as a broken image rather than executed. What the
check buys is not depending on those two headers staying correct forever, and
refusing a corrupt or mislabelled file at the door instead of storing it and
discovering it when somebody tries to read the receipt.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError

#: Evidence documents: a scan, a photo of a receipt, or a supplier's PDF.
DOCUMENT_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "webp"})

#: What a support ticket may carry: evidence plus the spreadsheets and screenshots
#: people actually attach to a bug report. Mirrors ``core.storage.ALLOWED_EXTENSIONS``
#: (minus the leading dots), which is what vs_tickets validated against before.
TICKET_EXTENSIONS = DOCUMENT_EXTENSIONS | frozenset({"gif", "csv", "xlsx", "xls"})

#: A school's own logo. Narrower than DOCUMENT_EXTENSIONS on purpose: this file is
#: rendered inline in the app shell and the browser favicon, so a PDF has no meaning
#: here, and GIF is left out because an animated logo in a sidebar is nobody's intent.
LOGO_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp"})

#: 5 MB. Comfortable for a phone photo of a receipt, far below the storage ceiling.
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024

#: 2 MB. A logo is a small image shown at 30px in a sidebar; anything larger is a
#: photograph somebody has not resized, and it is served on every page load.
MAX_LOGO_BYTES = 2 * 1024 * 1024

#: 10 MB. A support ticket carries logs and spreadsheets, not just a photo.
MAX_TICKET_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: Extension → the content type we will record and later serve it as. Deliberately a
#: fixed map rather than ``mimetypes.guess_type``/the browser-supplied value: the
#: content type is what MediaView serves the bytes as, so it must follow the verified
#: magic bytes, never the uploader's claim.
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
}

#: Extensions with no byte signature to check. CSV is plain text - there is nothing to
#: verify, and inventing a heuristic ("does it contain commas?") would reject valid
#: single-column files. Listed explicitly so the default stays fail-closed: an
#: extension nobody has thought about fails ``_magic_ok`` rather than sailing through.
_UNVERIFIABLE = frozenset({"csv"})


def _magic_ok(suffix: str, head: bytes) -> bool:
    """Does the leading byte signature match the claimed extension?"""
    if suffix in _UNVERIFIABLE:
        return True
    if suffix == "pdf":
        return head.startswith(b"%PDF")
    if suffix == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {"jpg", "jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == "webp":
        return head.startswith(b"RIFF") and b"WEBP" in head
    if suffix == "gif":
        return head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    if suffix == "xlsx":
        # An .xlsx is a zip; the same signature covers every OOXML container.
        return head.startswith(b"PK\x03\x04")
    if suffix == "xls":
        # Legacy OLE2 compound-file header.
        return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    return False


def validate_upload(
    upload,
    *,
    allowed=DOCUMENT_EXTENSIONS,
    max_bytes=MAX_DOCUMENT_BYTES,
    field="file",
    size_message=None,
    type_message=None,
) -> tuple[str, str]:
    """Validate an uploaded file and return ``(safe_name, content_type)``.

    Raises DRF :class:`~rest_framework.exceptions.ValidationError` keyed on ``field``
    so the caller answers 400, not 500. ``size_message`` and ``type_message`` override
    the default wording where an endpoint already advertises a specific limit or file
    list to its users, so adopting this helper does not silently reword its API.

    The returned name is stripped of characters that would break a
    ``Content-Disposition`` header and truncated to the 255 the model columns hold; it
    is the *display* name only. The stored path is chosen by the model's ``upload_to``
    and then given a high-entropy suffix by ``DatabaseStorage.get_available_name``.
    """
    if upload is None:
        raise ValidationError({field: "A file is required."})

    name = "".join(
        ch for ch in str(getattr(upload, "name", "") or "attachment")
        if ch.isprintable() and ch not in {'"', "\\"}
    )
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in allowed:
        readable = ", ".join(sorted(ext.upper() for ext in allowed))
        raise ValidationError({field: type_message or f"Upload one of: {readable}."})

    size = getattr(upload, "size", None)
    if size is None:
        raise ValidationError({field: "The upload is empty or unreadable."})
    if size <= 0:
        raise ValidationError({field: "The file is empty."})
    if size > max_bytes:
        raise ValidationError(
            {field: size_message or f"Each file must be {max_bytes // (1024 * 1024)}MB or smaller."}
        )

    # Read only the header, then rewind: the caller still has to save these bytes.
    head = upload.read(16)
    upload.seek(0)
    if not _magic_ok(suffix, head):
        raise ValidationError({field: "The file content does not match its extension."})

    return name[:255], _CONTENT_TYPES[suffix]
