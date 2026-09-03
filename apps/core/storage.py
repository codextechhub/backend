"""
Database-backed Django file storage (B9).

Configured as STORAGES["default"], so every FileField/ImageField
(import batches, school logos, staff photos) reads and writes through the
``StoredFile`` table instead of the ephemeral local disk.

Scope guard: the platform only accepts spreadsheets and images, so the
storage enforces an extension allowlist and a size ceiling as
defense-in-depth - serializer-level validation remains the first line.

Files are served by ``core.views.MediaView`` at ``/media/<name>`` (the URL
this storage hands back), which requires authentication.

Access model. Every stored file is bound to a tenant here, at write time, and to
its owning record a moment later (:mod:`core.binding`); ``MediaView`` then refuses
any read whose tenant, owning record or signature does not agree with the caller.
See :mod:`core.media` for what each of those checks catches.

The high-entropy name (:meth:`get_available_name`) survives as defence in depth,
not as the access control it used to be. It stops a path like
``expense-receipts/receipt.pdf`` from being typed into a browser; it never stopped
a name that had been handed out once from working for ever, for anyone.
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

# 25 MB default ceiling - far above any sane logo/photo/import sheet.
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
                f"File type '{ext or 'unknown'}' is not accepted - only "
                f"spreadsheets (csv/xlsx), images and PDFs are stored."
            )
        data = content.read()
        max_bytes = getattr(settings, "MEDIA_DB_MAX_BYTES", MAX_BYTES_DEFAULT)
        if len(data) > max_bytes:
            raise ValidationError(
                f"File is {len(data)} bytes - the upload ceiling is {max_bytes}."
            )
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # Stamp the customer while we still know it. The storage API never sees
        # the model instance, but the request's tenant is already in context for
        # auditing, and it is the one fact every upload path has in common.
        from vs_tenants.context import get_current_audit_identity, get_current_tenant

        tenant = get_current_tenant()
        _actor, effective, _session = get_current_audit_identity()
        creator = effective if getattr(effective, "pk", None) else None
        self._model.objects.update_or_create(
            name=name,
            defaults={
                "content": data,
                "content_type": content_type,
                "size": len(data),
                "tenant": tenant,
                "created_by": creator,
                # A name being written again is a live file, whatever an
                # earlier row of the same name once was.
                "revoked_at": None,
            },
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
        """The unsigned path. Not fetchable on its own - see :func:`core.media.signed_url`.

        Django calls this from ``FieldFile.url``, which has no idea who is asking,
        so it cannot mint the per-user signature ``MediaView`` requires. It is kept
        for templates and tooling that just need the storage path; anything handing
        a URL to a caller must go through ``core.media.signed_url`` instead.
        """
        return f"{settings.MEDIA_URL}{_clean_name(name)}"

    def get_available_name(self, name, max_length=None):
        # A high-entropy token in every stored filename, so a file cannot be
        # fetched by guessing a path. Defence in depth, not the access control:
        # that is the file's tenant, its owning record's policy and the URL
        # signature (see core.media). The original root stays as a readable prefix.
        name = _clean_name(name)
        directory, base = posixpath.split(name)
        root, ext = os.path.splitext(base)
        token = secrets.token_hex(8)  # 64 bits of entropy
        tokened = f"{root}-{token}{ext}" if root else f"{token}{ext}"
        final = posixpath.join(directory, tokened) if directory else tokened
        # super() keeps the default collision-suffix as a belt-and-braces uniquifier.
        return super().get_available_name(final, max_length=max_length)
