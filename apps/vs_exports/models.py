"""Export Centre object model.

Four things, in the order a person meets them:

:class:`ExportDefinition`
    The *recipe* - dataset, entity, columns, filters, format, file name. Editing it
    changes future files only; files already produced are never altered.

:class:`ExportRun`
    One attempt to produce a file. Carries ``frozen_config`` - the configuration as it
    was *at run time* - which is mandatory, not optional: it is the only way the run
    detail can honestly say what produced this file, and the only way the UI can show
    how the definition has drifted since.

:class:`ExportFile`
    The bytes and their availability. Expiry is derived from ``available_until`` at
    read time, so nothing depends on a sweeper being punctual and the run record is
    never rewritten to represent expiry.

:class:`ExportDownload`
    Who took the file, allowed *and* refused. A refusal leaves the same trail as a
    success, because "who tried and was told no" is what a compliance review asks.
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .constants import (
    DownloadOutcome,
    DownloadRefusal,
    ExportFormat,
    FailureCode,
    FILE_RETENTION_DAYS,
    MAX_CONSECUTIVE_FAILURES,
    PauseReason,
    Recurrence,
    RunPhase,
    RunStatus,
    RunTrigger,
    ScheduleState,
    Sharing,
    SUCCESSFUL_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ValuesMode,
)


class TimeStampedModel(models.Model):
    """Created/updated stamps, matching the convention in the other apps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------- #
# Definitions                                                                 #
# --------------------------------------------------------------------------- #
class ExportDefinition(TimeStampedModel):
    """A saved, reusable export recipe owned by one person.

    The tenant is always the boundary. An *entity* is an additional, narrower boundary
    that only some datasets have: finance, payments and procurement rows belong to a
    set of books, audit events and users do not. Which applies is declared by the
    dataset (:attr:`vs_exports.catalogue.Dataset.scope`), never assumed here.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="export_definitions",
    )
    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT,
        related_name="export_definitions", null=True, blank=True,
        help_text=(
            "The set of books this export reads, for entity-scoped datasets only. "
            "Null for tenant-scoped datasets, which cover the whole organisation."
        ),
    )
    dataset_key = models.CharField(
        max_length=100,
        help_text="Key into vs_exports.catalogue, e.g. 'finance.customer_invoices'.",
    )
    name = models.CharField(max_length=180)
    description = models.CharField(max_length=500, blank=True, default="")

    columns = models.JSONField(
        default=list,
        help_text="Ordered list of catalogue field ids - this is the column order in the file.",
    )
    filters = models.JSONField(
        default=list,
        help_text="List of filter specs, each {id, ...} per the filter's kind.",
    )
    sort = models.JSONField(
        default=list, blank=True,
        help_text="Ordered list of {field, direction} - direction is 'asc' or 'desc'.",
    )

    format = models.CharField(
        max_length=8, choices=ExportFormat.choices, default=ExportFormat.XLSX,
    )
    format_options = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Options for THIS format only, discriminated by format rather than a flat "
            "bag of nullable fields. Unknown keys are dropped on save."
        ),
    )
    values_mode = models.CharField(
        max_length=8, choices=ValuesMode.choices, default=ValuesMode.PEOPLE,
    )
    file_name_pattern = models.CharField(
        max_length=180, default="export-{date}",
        help_text="Supports {date} {datetime} {entity} {run} tokens.",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="export_definitions",
        help_text="Runs execute as the owner; sharing never widens what the data shows.",
    )
    sharing = models.CharField(
        max_length=8, choices=Sharing.choices, default=Sharing.PRIVATE,
    )
    is_draft = models.BooleanField(
        default=False, help_text="A recipe that is not finished - visible, not runnable.",
    )
    is_archived = models.BooleanField(default=False)


    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "-updated_at"]),
            models.Index(fields=["entity", "dataset_key"]),
            models.Index(fields=["owner", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.dataset_key})"

    @property
    def dataset(self):
        """The catalogue entry, or ``None`` when it has been withdrawn."""
        from .catalogue import get_dataset

        return get_dataset(self.dataset_key)

    # Render the configured file name for one run.
    def render_file_name(self, *, run_id=None, when=None) -> str:
        """Expand the name pattern's tokens. The extension is added by the writer."""
        when = when or timezone.localtime()
        tokens = {
            "date": when.strftime("%Y-%m-%d"),
            "datetime": when.strftime("%Y-%m-%d-%H%M"),
            # Tenant-scoped exports have no entity; the token resolves to the tenant
            # slug so a name pattern is never left with a literal "{entity}" in it.
            "entity": (
                (self.entity.code or "").lower() if self.entity_id
                else (self.tenant.slug or "").lower()
            ),
            "run": str(run_id or ""),
        }
        name = self.file_name_pattern or "export-{date}"
        for token, value in tokens.items():
            name = name.replace("{" + token + "}", value)
        # Keep the name safe for a Content-Disposition header and for disk.
        return "".join(c for c in name if c.isalnum() or c in "-_.") or "export"


class ExportDefinitionShare(TimeStampedModel):
    """One person a definition is shared with.

    Sharing grants sight of the definition and its files. It never grants the sharer's
    data access: the run always executes as the owner, and every download is
    re-authorised against the downloader.
    """

    definition = models.ForeignKey(
        ExportDefinition, on_delete=models.CASCADE, related_name="shares",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="export_shares",
    )
    shared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "user"], name="uniq_export_share_definition_user",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.definition_id} → {self.user_id}"


# --------------------------------------------------------------------------- #
# Schedules                                                                   #
# --------------------------------------------------------------------------- #
class ExportSchedule(TimeStampedModel):
    """When a definition runs on its own.

    Local time plus a timezone name, never a stored UTC offset - that is what keeps a
    03:00 Lagos run at 03:00 when the clocks move. ``next_run_at`` is materialised in
    UTC purely so the dispatcher has something to index; it is always recomputed from
    the local fields by :meth:`reschedule`.

    A schedule runs as the *definition's owner*, not as whoever created the schedule,
    so an unattended run can never read more than the owner could read by hand.
    """

    definition = models.ForeignKey(
        ExportDefinition, on_delete=models.CASCADE, related_name="schedules",
    )
    recurrence = models.CharField(max_length=12, choices=Recurrence.choices)
    day = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=(
            "1-31 for MONTHLY/QUARTERLY, 0-6 (Monday-Sunday) for WEEKLY, ignored "
            "otherwise. A day past the end of a short month runs on its last day."
        ),
    )
    at_time = models.TimeField(help_text="Local wall-clock time in `timezone_name`.")
    timezone_name = models.CharField(max_length=64, default="Africa/Lagos")

    starts_on = models.DateField()
    ends_on = models.DateField(
        null=True, blank=True, help_text="Null means no end date.",
    )

    skip_when_empty = models.BooleanField(
        default=False,
        help_text=(
            "Produce no file when nothing matched, instead of an empty one. Off by "
            "default: an empty file is itself evidence that nothing happened, and a "
            "missing file is indistinguishable from a schedule that never ran."
        ),
    )

    state = models.CharField(
        max_length=10, choices=ScheduleState.choices, default=ScheduleState.ACTIVE,
    )
    pause_reason = models.CharField(
        max_length=24, choices=PauseReason.choices, blank=True, default="",
    )
    pause_detail = models.CharField(max_length=300, blank=True, default="")
    consecutive_failures = models.PositiveSmallIntegerField(default=0)

    next_run_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Derived from the local fields; the dispatcher's only index.",
    )
    last_run = models.ForeignKey(
        "ExportRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["next_run_at"]
        indexes = [models.Index(fields=["state", "next_run_at"])]

    def __str__(self) -> str:
        return f"{self.definition_id} - {self.recurrence}"

    # Recompute and persist the next occurrence.
    def reschedule(self, *, after=None, save: bool = True):
        """Set ``next_run_at`` to the first occurrence strictly after ``after``.

        A series that has run out becomes FINISHED rather than PAUSED: paused means a
        person must act, and a completed one-off needs nobody.
        """
        from .scheduling import next_occurrence

        self.next_run_at = next_occurrence(self, after=after)
        if self.next_run_at is None and self.state == ScheduleState.ACTIVE:
            self.state = ScheduleState.FINISHED
        if save:
            self.save(update_fields=["next_run_at", "state", "updated_at"])
        return self.next_run_at

    # Record a failed run and auto-pause once the threshold is crossed.
    def register_failure(self, detail: str = ""):
        """The third consecutive failure pauses the schedule and records why.

        Without this a broken export quietly produces one identical failure a night
        for a month, and the Files list stops being readable.
        """
        self.consecutive_failures += 1
        fields = ["consecutive_failures", "updated_at"]
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.state = ScheduleState.PAUSED
            self.pause_reason = PauseReason.CONSECUTIVE_FAILURES
            self.pause_detail = detail[:300]
            fields += ["state", "pause_reason", "pause_detail"]
        self.save(update_fields=fields)

    # Clear the failure counter after a good run.
    def register_success(self):
        if self.consecutive_failures:
            self.consecutive_failures = 0
            self.save(update_fields=["consecutive_failures", "updated_at"])


# --------------------------------------------------------------------------- #
# Runs                                                                        #
# --------------------------------------------------------------------------- #
class ExportRun(TimeStampedModel):
    """One attempt to produce a file.

    ``frozen_config`` is the whole point of this row: it records dataset, entity,
    columns, filters, format and values mode exactly as they were when the run began,
    so the run detail describes *this* file rather than whatever the definition has
    since become.
    """

    reference = models.CharField(
        max_length=48, unique=True,
        help_text="Tenant-level daily run reference, e.g. XR-12608291.",
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="export_runs",
    )
    entity = models.ForeignKey(
        "vs_finance.LedgerEntity", on_delete=models.PROTECT, related_name="export_runs",
        null=True, blank=True,
        help_text="Set for entity-scoped datasets only; null for tenant-scoped ones.",
    )
    definition = models.ForeignKey(
        ExportDefinition, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
        help_text="Null for a quick export, which never had a saved recipe.",
    )
    schedule = models.ForeignKey(
        ExportSchedule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
        help_text="Set when a schedule started this run rather than a person.",
    )

    frozen_config = models.JSONField(
        help_text="The configuration used at run time. Mandatory, never back-filled.",
    )
    trigger = models.CharField(
        max_length=10, choices=RunTrigger.choices, default=RunTrigger.MANUAL,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="export_runs",
        help_text="The actor who asked for the run.",
    )
    client_key = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Idempotency key: the same key inside the window returns this run.",
    )

    status = models.CharField(
        max_length=26, choices=RunStatus.choices, default=RunStatus.QUEUED,
    )
    phase = models.CharField(
        max_length=12, choices=RunPhase.choices, default=RunPhase.QUEUED,
    )
    rows_done = models.PositiveIntegerField(default=0)
    rows_total = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Null means the UI renders indeterminate progress - expected, not an error.",
    )
    row_count = models.PositiveIntegerField(
        null=True, blank=True, help_text="Rows actually written.",
    )

    omissions = models.JSONField(
        default=list, blank=True,
        help_text="Structured [{code, scope, detail, items[]}] behind COMPLETED_WITH_OMISSIONS.",
    )
    failure_code = models.CharField(
        max_length=32, choices=FailureCode.choices, blank=True, default="",
    )
    failure_message = models.TextField(
        blank=True, default="", help_text="User-safe. Never a traceback.",
    )
    attempt = models.PositiveSmallIntegerField(default=1)
    cancel_requested = models.BooleanField(
        default=False, help_text="Cancellation is cooperative: the worker checks this.",
    )

    background_job = models.ForeignKey(
        "core.BackgroundJob", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-queued_at"]
        indexes = [
            models.Index(fields=["tenant", "-queued_at"]),
            models.Index(fields=["definition", "-queued_at"]),
            models.Index(fields=["status", "-queued_at"]),
            models.Index(fields=["requested_by", "-queued_at"]),
            models.Index(fields=["client_key"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} [{self.status}]"

    # Allocate a short, quotable reference from the platform's locked tenant sequence.
    @staticmethod
    def new_reference(tenant) -> str:
        from vs_tenants.numbering import next_tenant_document_number

        return next_tenant_document_number(tenant=tenant, document_code="XR")

    def save(self, *args, **kwargs):
        if not self.reference and self.tenant_id:
            # Keep the counter advance and the run insert in one transaction. If the
            # run is rejected, its number remains available rather than leaving a gap.
            with transaction.atomic():
                self.reference = self.new_reference(self.tenant)
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {"reference"}
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCESSFUL_RUN_STATUSES

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.ended_at:
            return None
        return (self.ended_at - self.started_at).total_seconds()

    # The boundary this run reads inside.
    def scope_context(self):
        """The :class:`~vs_exports.catalogue.ScopeContext` this run executes against.

        Built from the run's own columns rather than the definition's, so a definition
        moved to another entity after the fact cannot change what an old run reports
        having read.

        ``user`` is the one part not read off this row, and deliberately: a run
        executes *as* the definition's owner, or as whoever requested an ad-hoc
        one, and :func:`vs_exports.services.execute_run` has already refused to
        start if that person is no longer active. Branch narrowing therefore
        resolves against the grants they hold when the file is built, which is
        the same person and the same moment the column narrowing already uses.
        """
        from .catalogue import ScopeContext

        owner = self.definition.owner if self.definition_id else self.requested_by
        return ScopeContext(
            tenant=self.tenant, entity=self.entity, user=owner or self.requested_by,
        )

    @property
    def failure_guidance(self) -> str:
        """The recommended action for this run's failure code."""
        from .constants import FAILURE_GUIDANCE

        return FAILURE_GUIDANCE.get(self.failure_code, "") if self.failure_code else ""


# --------------------------------------------------------------------------- #
# Files and downloads                                                         #
# --------------------------------------------------------------------------- #
class ExportFile(TimeStampedModel):
    """The produced bytes, plus how long they stay available.

    Availability is computed from ``available_until`` on read. A nightly job purges
    storage and sets ``purged_at``; nothing about the *run* changes when a file
    expires, because the run genuinely did succeed.
    """

    run = models.OneToOneField(
        ExportRun, on_delete=models.CASCADE, related_name="file",
    )
    name = models.CharField(max_length=255, help_text="File name including extension.")
    format = models.CharField(max_length=8, choices=ExportFormat.choices)
    storage_name = models.CharField(
        max_length=255, help_text="Key into the platform's default storage backend.",
    )
    size_bytes = models.PositiveBigIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)
    columns_produced = models.JSONField(
        default=list,
        help_text="Field ids actually written - may be fewer than the run requested.",
    )
    available_until = models.DateTimeField(db_index=True)
    purged_at = models.DateTimeField(null=True, blank=True)
    download_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["available_until"]),
        ]

    def __str__(self) -> str:
        return self.name

    # Default expiry for a file produced now.
    @staticmethod
    def default_expiry(from_time=None):
        return (from_time or timezone.now()) + datetime.timedelta(days=FILE_RETENTION_DAYS)

    @property
    def is_expired(self) -> bool:
        """Derived at read time - never a stored status."""
        return timezone.now() >= self.available_until

    @property
    def is_purged(self) -> bool:
        return self.purged_at is not None

    @property
    def is_downloadable(self) -> bool:
        return not self.is_expired and not self.is_purged


class ExportDownload(models.Model):
    """Every download attempt on a file - allowed and refused alike.

    Refusals are logged because "who tried and was told no" is exactly the question a
    compliance review asks, and a refusal that leaves no trace cannot be answered.
    """

    file = models.ForeignKey(
        ExportFile, on_delete=models.CASCADE, related_name="downloads",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="export_downloads",
    )
    at = models.DateTimeField(auto_now_add=True)
    ip_address = models.CharField(max_length=45, blank=True, default="")
    outcome = models.CharField(max_length=8, choices=DownloadOutcome.choices)
    refusal_reason = models.CharField(
        max_length=24, choices=DownloadRefusal.choices, blank=True, default="",
    )

    class Meta:
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["file", "-at"]),
            models.Index(fields=["user", "-at"]),
        ]

    def __str__(self) -> str:
        return f"{self.file_id} {self.outcome} @{self.at:%Y-%m-%d %H:%M}"


# --------------------------------------------------------------------------- #
# Analytics                                                                   #
# --------------------------------------------------------------------------- #
class ExportAnalyticsEvent(models.Model):
    """One product-analytics event - the pipeline that sits *beside* the audit trail.

    Deliberately unlike :class:`vs_audit.models.AuditEvent`: prunable rather than
    immutable, kept for :data:`vs_exports.analytics.RETENTION_DAYS` rather than
    forever, and carrying only counts and bucket labels. ``properties`` is filtered
    through :data:`vs_exports.analytics.SCHEMA` on the way in, so no row data or field
    value can reach it - see :mod:`vs_exports.analytics` for why the two pipelines are
    separate.

    There is no entity column on purpose. This measures *behaviour*, not data, so the
    boundary that matters is the tenant.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.CASCADE, related_name="export_analytics",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="export_analytics",
        help_text="Nullable so a deleted account does not take the metric with it.",
    )
    name = models.CharField(
        max_length=48, db_index=True,
        help_text="A key from vs_exports.analytics.Event. Unknown names are dropped.",
    )
    properties = models.JSONField(
        default=dict, blank=True,
        help_text="Whitelisted, bucketed scalars only. Never row data or field values.",
    )
    session_key = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Opaque client-generated id, so one builder session can be stitched.",
    )
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["tenant", "name", "-occurred_at"]),
            models.Index(fields=["session_key"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} @{self.occurred_at:%Y-%m-%d %H:%M}"
