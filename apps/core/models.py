"""
Backing model for the database-backed media storage (core.storage).

The platform only ever receives two kinds of uploads — import spreadsheets
(CSV/XLSX) and images (school logos, staff photos) — all small. Storing them
in the database means uploads survive ephemeral-disk redeploys, ride along
with normal DB backups, and need no object-storage account. If volume ever
outgrows this, point STORAGES["default"] at S3 and migrate the rows out.
"""
from __future__ import annotations

from django.db import models


class StoredFile(models.Model):
    """One uploaded file, addressed by its storage name (the upload path)."""

    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.size}B)"
