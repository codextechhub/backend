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
"""
from __future__ import annotations

import mimetypes
import os
import posixpath

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
        # update_or_create in _save makes saves idempotent per name; still
        # uniquify like the default storage so parallel uploads don't clash.
        return super().get_available_name(_clean_name(name), max_length=max_length)
