"""
Backing model for the database-backed media storage (core.storage).

The platform only ever receives two kinds of uploads - import spreadsheets
(CSV/XLSX) and images (school logos, staff photos) - all small. Storing them
in the database means uploads survive ephemeral-disk redeploys, ride along
with normal DB backups, and need no object-storage account. If volume ever
outgrows this, point STORAGES["default"] at S3 and migrate the rows out.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models


class StoredFile(models.Model):
    """One uploaded file, and the record it belongs to.

    The binding columns are not bookkeeping, they are the authorisation input.
    A row used to be nothing but a name and some bytes, which made the name the
    only credential: anyone signed in who had ever seen a ``/media/<name>`` URL
    could fetch it for ever, from any tenant, whatever had since happened to the
    record it came from. ``core.media.authorize`` now answers three questions
    that only these columns can answer - whose file is this, what record is it
    evidence for, and is it still current - and refuses the read unless all
    three agree with the caller.

    ``tenant`` is stamped by :class:`core.storage.DatabaseStorage` from the
    request's tenant context at save time. ``owner_*`` is stamped just after the
    owning row is saved (see :mod:`core.binding`), because a new record has no
    primary key yet while its file is being written.
    """

    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.CASCADE, null=True, blank=True,
        related_name="stored_files",
        help_text=(
            "The customer whose file this is. Null means the bytes were written "
            "with no tenant in context (a management command, a scheduled job); "
            "such a row is never served through /media/."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stored_files",
        help_text="Whoever was acting when the bytes were written, for audit.",
    )

    owner_content_type = models.ForeignKey(
        # SET_NULL, not CASCADE. Django prunes stale content types when a model is
        # renamed or removed, and cascading would take the bytes with them: silent
        # data loss triggered by a refactor. Losing the binding is enough, because
        # an unbound row is refused and the file stops being served either way.
        "contenttypes.ContentType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stored_files",
    )
    owner_object_id = models.CharField(max_length=64, blank=True, default="")
    owner_field = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Which file field on the owning record points here.",
    )
    owner = GenericForeignKey("owner_content_type", "owner_object_id")

    revoked_at = models.DateTimeField(
        null=True, blank=True,
        help_text=(
            "Set when the file stops being current - superseded by a re-upload, "
            "or its record deleted. Revoking empties the bytes and closes the "
            "URL; it does not remove the row, so an audit still shows the file "
            "existed."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["tenant"]),
            models.Index(fields=["owner_content_type", "owner_object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.size}B)"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class BackgroundJob(models.Model):
    """User-facing record of one asynchronous operation (the "queue" row).

    Whoever triggers an async task - CX staff or school user - gets a row
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
        help_text=(
            "The ACTOR who triggered the task - never the subject the task acts "
            "on. An invitation email to Jane queued by admin Ada is owned by Ada. "
            "Null for system/scheduled runs."
        ),
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
    notify_owner = models.BooleanField(
        default=True,
        help_text=(
            "Whether the owner gets an in-app notification when the task ends. "
            "False for per-recipient fan-out plumbing (one invitation email per "
            "imported row), where one notification per job would spam the actor "
            "- the row is still tracked in View Queues."
        ),
    )
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


class TaskDiagnostic(models.Model):
    """The unredacted failure record for one background task.

    ``BackgroundJob`` is the operational surface: it is listed, filtered,
    paginated and serialised to school users through ``/v1/user/me/tasks/``.
    Its ``error``, ``traceback`` and ``result`` columns are therefore written
    REDACTED (see ``core.redaction``), which keeps guardians' email addresses
    out of screens, backups and the school-facing API.

    The raw text still has to exist. An engineer at 2am needs the real
    exception, and an auditor asking what happened last quarter needs it long
    after Render has dropped its own logs. That is this table: one row per
    failure, written by ``TrackedTask._finish``, never listed, never joined
    into an ordinary response, readable only through
    ``/v1/admin/tasks/<id>/diagnostics/`` behind
    ``platform.tasks.view_sensitive``, and audited on every read.

    So the raw text is not destroyed, it is *moved*: from a surface every CX
    account can browse to one a named key opens and the audit trail records.

    Retention is deliberately longer than the ``BackgroundJob`` prune (90
    days) because the auditor's window is longer than the engineer's. It is
    enforced by ``core.tasks.prune_task_diagnostics_task`` against
    ``expires_at``, which is stamped once at write time so changing the
    setting later never silently extends the life of rows already written.
    """

    #: Default retention. Overridable with ``TASK_DIAGNOSTIC_RETENTION_DAYS``.
    DEFAULT_RETENTION_DAYS = 400

    job = models.OneToOneField(
        "core.BackgroundJob", on_delete=models.CASCADE,
        related_name="diagnostic",
    )
    # Denormalised from the job so retention and tenant-scoped reads never
    # need the join, and so the row still names its customer if the job is
    # pruned out from under it.
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.CASCADE,
        related_name="task_diagnostics",
    )
    task_name = models.CharField(max_length=255, blank=True, default="")
    raw_error = models.TextField(blank=True, default="")
    raw_traceback = models.TextField(blank=True, default="")
    raw_result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        db_index=True,
        help_text="Stamped at write time from TASK_DIAGNOSTIC_RETENTION_DAYS.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"diagnostic for {self.task_name} ({self.job_id})"

    @classmethod
    def retention_days(cls) -> int:
        from django.conf import settings

        return int(
            getattr(settings, "TASK_DIAGNOSTIC_RETENTION_DAYS", cls.DEFAULT_RETENTION_DAYS)
        )
