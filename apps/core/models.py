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


class BackgroundJob(models.Model):
    """User-facing record of one asynchronous operation (the "queue" row).

    Whoever triggers an async task — CX staff or school user — gets a row
    here they can track: when it started, when it finished, what came out.
    System/scheduled runs are recorded with owner=None so admins see the
    full queue. Created/updated automatically by core.tasks_base.TrackedTask.
    """

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    owner = models.ForeignKey(
        "vs_user.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="background_jobs",
        help_text="Who triggered the task. Null for system/scheduled runs.",
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="background_jobs",
    )
    kind = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Short category for filtering: import, export, email, system…",
    )
    label = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Human description shown in the queue UI.",
    )
    task_name = models.CharField(max_length=255, blank=True, default="")
    celery_task_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.QUEUED,
    )
    progress = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="0–100 when the task reports progress.",
    )
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    traceback = models.TextField(blank=True, default="")
    worker = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["kind", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.label or self.task_name} [{self.status}]"
