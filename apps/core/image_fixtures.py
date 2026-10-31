"""Real picture bytes for tests that upload a photograph.

Photograph uploads are opened with Pillow (``vs_students.serializers.check_upload``),
so a test that sends ``b"x" * 32`` labelled ``image/jpeg`` is refused, as it
should be. These make the smallest files Pillow accepts.
"""
from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile

#: Pillow's format name for each extension a photograph may carry.
_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def image_bytes(fmt="JPEG", size=(8, 8)) -> bytes:
    """A small solid picture in *fmt* ("JPEG", "PNG" or "WEBP")."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(out, format=fmt)
    return out.getvalue()


def photo(name="face.jpg", content_type=None) -> SimpleUploadedFile:
    """An upload whose bytes really are the picture its extension names."""
    fmt = _FORMATS[name.rsplit(".", 1)[-1].lower()]
    return SimpleUploadedFile(name, image_bytes(fmt), content_type=content_type or _TYPES[fmt])
