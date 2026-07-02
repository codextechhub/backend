"""
Database-backed Django file storage (B9).

Configured as STORAGES["default"], so every FileField/ImageField
(import batches, school logos, staff photos) reads and writes through the
``StoredFile`` table instead of the ephemeral local disk.

Scope guard: the platform only accepts spreadsheets and images, so the
storage enforces an extension allowlist and a size ceiling as
defense-in-depth — serializer-level validation remains the first line.

Files are served by ``core.views.MediaView`` at ``/media/<name>`` (the URL
this storage hands back), which requires authentication.

Access model — **capability URLs.** ``MediaView`` authenticates the caller but
cannot authorise per-file (the ``StoredFile`` row has no owner/entity). To stop a
logged-in user from fetching another tenant's file by guessing a predictable path
(e.g. ``expense-receipts/receipt.pdf``), every stored file's name carries a
high-entropy token (:meth:`DatabaseStorage.get_available_name`). A name is therefore
effectively unguessable and is only ever handed out to callers already allowed to see
the owning record (the API embeds it in that record's serialized ``*_url``).
"""
from __future__ import annotations

import mimetypes
import os
import posixpath
import secrets

from django.conf import settings
from django.core.exceptions import SuspiciousOperation, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

ALLOWED_EXTENSIONS = {
    ".csv", ".xlsx", ".xls",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf",  # supporting documents (e.g. expense receipts)
}

# 25 MB default ceiling — far above any sane logo/photo/import sheet.
MAX_BYTES_DEFAULT = 25 * 1024 * 1024


def _clean_name(name: str) -> str:
    name = posixpath.normpath(name.replace("\\", "/")).lstrip("/")
    if name.startswith("..") or "/../" in name:
        raise SuspiciousOperation(f"Unsafe storage path: {name!r}")
    return name


@deconstructible
class DatabaseStorage(Storage):
    @property
    def _model(self):
        from .models import StoredFile

        return StoredFile

    # -- core protocol ------------------------------------------------------
    def _open(self, name, mode="rb"):
        name = _clean_name(name)
        try:
            row = self._model.objects.get(name=name)
        except self._model.DoesNotExist:
            raise FileNotFoundError(name)
        return ContentFile(bytes(row.content), name=name)

    def _save(self, name, content):
        name = _clean_name(name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValidationError(
                f"File type '{ext or 'unknown'}' is not accepted — only "
                f"spreadsheets (csv/xlsx), images and PDFs are stored."
            )
        data = content.read()
        max_bytes = getattr(settings, "MEDIA_DB_MAX_BYTES", MAX_BYTES_DEFAULT)
        if len(data) > max_bytes:
            raise ValidationError(
                f"File is {len(data)} bytes — the upload ceiling is {max_bytes}."
            )
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._model.objects.update_or_create(
            name=name,
            defaults={"content": data, "content_type": content_type, "size": len(data)},
        )
        return name

    # -- queries ------------------------------------------------------------
    def exists(self, name):
        return self._model.objects.filter(name=_clean_name(name)).exists()

    def delete(self, name):
        self._model.objects.filter(name=_clean_name(name)).delete()

    def size(self, name):
        row = self._model.objects.filter(name=_clean_name(name)).values("size").first()
        if row is None:
            raise FileNotFoundError(name)
        return row["size"]

    def url(self, name):
        return f"{settings.MEDIA_URL}{_clean_name(name)}"

    def get_available_name(self, name, max_length=None):
        # Capability-URL hardening: give every stored file a high-entropy token in its
        # name so it can't be fetched by guessing a predictable path. The media endpoint
        # authenticates the caller but authorises by *knowing the name*, which is only
        # ever handed to callers allowed to see the owning record. The original filename
        # root is kept as a readable prefix; the token supplies the unguessability.
        name = _clean_name(name)
        directory, base = posixpath.split(name)
        root, ext = os.path.splitext(base)
        token = secrets.token_hex(8)  # 64 bits of entropy
        tokened = f"{root}-{token}{ext}" if root else f"{token}{ext}"
        final = posixpath.join(directory, tokened) if directory else tokened
        # super() keeps the default collision-suffix as a belt-and-braces uniquifier.
        return super().get_available_name(final, max_length=max_length)
