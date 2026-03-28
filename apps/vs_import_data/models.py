from __future__ import annotations

import os
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from vs_institutions.models import Branch


# =========================================================
# Base model
# =========================================================
class TimeStampedModel(models.Model):
    """Abstract base that injects consistent created/updated timestamps into every import model."""
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# =========================================================
# Choices / constants
# =========================================================
class FileFormatChoices(models.TextChoices):
    CSV = "csv", "CSV"
    XLSX = "xlsx", "Excel (.xlsx)"
    XLS = "xls", "Excel (.xls)"


class DatasetTypeChoices(models.TextChoices):
    STUDENTS = "students", "Students"
    STAFF = "staff", "Staff"
    CLASSES = "classes", "Classes / Structure"
    FEES = "fees", "Fees"
    VENDORS = "vendors", "Vendors"
    HISTORICAL = "historical", "Historical Data"
    GENERIC = "generic", "Generic / Unknown"


class ImportBatchStatusChoices(models.TextChoices):
    DRAFT = "draft", "Draft"
    UPLOADED = "uploaded", "Uploaded"
    DETECTING = "detecting", "Detecting Dataset Type"
    MAPPING_REQUIRED = "mapping_required", "Mapping Required"
    VALIDATING = "validating", "Validating"
    VALIDATION_FAILED = "validation_failed", "Validation Failed"
    READY_TO_IMPORT = "ready_to_import", "Ready To Import"
    IMPORT_QUEUED = "import_queued", "Import Queued"
    IMPORT_RUNNING = "import_running", "Import Running"
    IMPORT_PARTIAL = "import_partial", "Import Partial"
    IMPORT_SUCCEEDED = "import_succeeded", "Import Succeeded"
    IMPORT_FAILED = "import_failed", "Import Failed"
    ROLLED_BACK = "rolled_back", "Rolled Back"
    CANCELLED = "cancelled", "Cancelled"


class MappingSourceChoices(models.TextChoices):
    AUTO = "auto", "Auto"
    MANUAL = "manual", "Manual"
    TEMPLATE = "template", "Template"


class ValidationSeverityChoices(models.TextChoices):
    ERROR = "error", "Error"
    WARNING = "warning", "Warning"
    INFO = "info", "Info"


class ValidationCodeChoices(models.TextChoices):
    FILE_TYPE_INVALID = "file_type_invalid", "File Type Invalid"
    FILE_EMPTY = "file_empty", "File Empty"
    SHEET_MISSING = "sheet_missing", "Sheet Missing"
    COLUMN_MISSING = "column_missing", "Required Column Missing"
    COLUMN_UNKNOWN = "column_unknown", "Unknown Column"
    REQUIRED_VALUE_MISSING = "required_value_missing", "Required Value Missing"
    INVALID_FORMAT = "invalid_format", "Invalid Format"
    INVALID_CHOICE = "invalid_choice", "Invalid Choice"
    DUPLICATE_RECORD = "duplicate_record", "Duplicate Record"
    CROSS_REFERENCE_MISSING = "cross_reference_missing", "Cross Reference Missing"
    DATASET_MISMATCH = "dataset_mismatch", "Dataset Type Mismatch"
    DUPLICATE_MAPPING = "duplicate_mapping", "Duplicate Mapping"
    BUSINESS_RULE = "business_rule", "Business Rule Violation"


class ImportJobStatusChoices(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    ROLLED_BACK = "rolled_back", "Rolled Back"


class ImportRowActionChoices(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    SKIP = "skip", "Skip"
    FAILED = "failed", "Failed"


class NotificationStatusChoices(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class TemplateStatusChoices(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    RETIRED = "retired", "Retired"


class TemplateColumnDataTypeChoices(models.TextChoices):
    STRING = "string", "String"
    INTEGER = "integer", "Integer"
    DECIMAL = "decimal", "Decimal"
    DATE = "date", "Date"
    DATETIME = "datetime", "Datetime"
    EMAIL = "email", "Email"
    BOOLEAN = "boolean", "Boolean"
    CHOICE = "choice", "Choice"


# =========================================================
# Upload path helper
# =========================================================
def import_file_upload_to(instance: "ImportBatch", filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    branch_code = getattr(instance.branch, "code", "branch")
    return (
        f"imports/{branch_code}/{instance.dataset_type or 'unknown'}/"
        f"{instance.id}{ext}"
    )

def import_template_file_upload_to(instance: "ImportTemplate", filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return f"system_import_templates/{instance.dataset_type}/{instance.code}{ext}"


# =========================================================
# Main import batch
# =========================================================
class ImportBatch(TimeStampedModel):
    """
    Represents a single uploaded data file and tracks it through the entire import lifecycle.

    The record holds the owning branch, uploader, original file metadata, detected structure,
    validation summaries, and lifecycle timestamps so both the UI and background workers can
    resume from any stage (upload → detection → mapping → validation → import → rollback).
    """

    id = models.AutoField(primary_key=True, editable=False)

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="import_batches",
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_import_batches",
    )

    template = models.ForeignKey(
        "ImportTemplate",
        on_delete=models.PROTECT,
        related_name="import_batches",
        null=True,
        blank=True,
        help_text="Official system template chosen for this import batch.",
    )

    file = models.FileField(upload_to=import_file_upload_to)

    status = models.CharField(
        max_length=40,
        choices=ImportBatchStatusChoices.choices,
        default=ImportBatchStatusChoices.UPLOADED,
    )

    file_size_bytes = models.BigIntegerField(default=0)
    total_rows = models.PositiveIntegerField(default=0)
    total_columns = models.PositiveIntegerField(default=0)
    header_row_index = models.PositiveIntegerField(default=1)

    # Optional sheet metadata for Excel files
    sheet_name = models.CharField(max_length=255, blank=True)

    # Parsed metadata / snapshots
    uploaded_headers = models.JSONField(default=list, blank=True)  # List of column headers as they appear in the uploaded file
    template_headers_snapshot = models.JSONField(default=list, blank=True)  # List of expected column headers from the template at the time of upload
    structure_matches_template = models.BooleanField(default=False)  # Indicates if the uploaded file structure matches the template

    has_critical_errors = models.BooleanField(default=False)
    is_ready_for_import = models.BooleanField(default=False)

    validation_started_at = models.DateTimeField(null=True, blank=True)
    validation_completed_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["branch", "dataset_type"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.branch} - {self.dataset_type} - {self.original_filename}"

    def clean(self):
        allowed = {FileFormatChoices.CSV, FileFormatChoices.XLSX, FileFormatChoices.XLS}
        if self.file_format not in allowed:
            raise ValidationError({"file_format": "Only CSV and Excel files are supported."})

    @property
    def error_count(self) -> int:
        return self.validation_issues.filter(severity=ValidationSeverityChoices.ERROR).count()

    @property
    def warning_count(self) -> int:
        return self.validation_issues.filter(severity=ValidationSeverityChoices.WARNING).count()


class ImportTemplate(TimeStampedModel):
    """
    Official system-defined import template.
    Since you are using only system templates, this becomes
    the single source of truth for expected file structure.
    """

    id = models.AutoField(primary_key=True, editable=False)

    code = models.CharField(
        max_length=100,
        unique=True,
        help_text="Stable internal code. Example: students_master_v1",
    )

    name = models.CharField(max_length=150)
    dataset_type = models.CharField(max_length=30, choices=DatasetTypeChoices.choices)

    description = models.TextField(blank=True)

    version = models.CharField(max_length=20, default="1.0")
    status = models.CharField(
        max_length=20,
        choices=TemplateStatusChoices.choices,
        default=TemplateStatusChoices.ACTIVE,
    )

    default_file_format = models.CharField(
        max_length=10,
        choices=FileFormatChoices.choices,
        default=FileFormatChoices.XLSX,
    )

    template_file = models.FileField(
        upload_to=import_template_file_upload_to,
        blank=True,
        null=True,
        help_text="Optional pre-generated downloadable file.",
    )

    instructions = models.TextField(
        blank=True,
        help_text="Plain-language instructions shown to the admin before download/upload.",
    )

    allow_sample_row = models.BooleanField(default=True)
    sample_row_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional example row shown in generated template file.",
    )

    validation_rules = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional dataset-wide rules or config.",
    )

    is_download_enabled = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_system_import_templates",
        null=True,
        blank=True,
    )

    published_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["dataset_type", "name", "-version"]
        indexes = [
            models.Index(fields=["dataset_type", "status"]),
            models.Index(fields=["code"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.version})"

    def clean(self):
        if self.status == TemplateStatusChoices.ACTIVE and not self.columns.exists():
            raise ValidationError("An active template must have at least one column.")


class ImportTemplateColumn(TimeStampedModel):
    """
    Defines each column that must appear in the downloadable/uploadable template.
    This replaces the need for dynamic per-batch mappings in the system-template flow.
    """

    id = models.AutoField(primary_key=True, editable=False)

    template = models.ForeignKey(
        ImportTemplate,
        on_delete=models.CASCADE,
        related_name="columns",
    )

    column_name = models.CharField(
        max_length=255,
        help_text="Exact spreadsheet header expected in uploaded file.",
    )

    target_field = models.CharField(
        max_length=255,
        help_text="Internal field the import engine uses.",
    )

    display_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Friendly name shown in docs/UI if needed.",
    )

    help_text = models.TextField(
        blank=True,
        help_text="Explains what the admin should put in this column.",
    )

    data_type = models.CharField(
        max_length=20,
        choices=TemplateColumnDataTypeChoices.choices,
        default=TemplateColumnDataTypeChoices.STRING,
    )

    is_required = models.BooleanField(default=False)
    is_unique = models.BooleanField(default=False)

    max_length = models.PositiveIntegerField(null=True, blank=True)

    allowed_values = models.JSONField(
        default=list,
        blank=True,
        help_text="Used when data_type=choice or where fixed values are expected.",
    )

    sample_value = models.CharField(max_length=255, blank=True)
    default_value = models.CharField(max_length=255, blank=True)

    column_order = models.PositiveIntegerField(default=1)

    # Optional reference metadata for cross-validation
    reference_model = models.CharField(
        max_length=255,
        blank=True,
        help_text="Example: Staff, Campus, ClassRoom",
    )
    reference_lookup_field = models.CharField(
        max_length=255,
        blank=True,
        help_text="Example: full_name, name, code",
    )

    class Meta:
        ordering = ["column_order", "column_name"]
        unique_together = [
            ("template", "column_name"),
            ("template", "target_field"),
        ]
        indexes = [
            models.Index(fields=["template", "column_order"]),
            models.Index(fields=["template", "is_required"]),
        ]

    def __str__(self) -> str:
        return f"{self.template.code} - {self.column_name}"


# =========================================================
# Validation results
# =========================================================
class ImportValidationIssue(TimeStampedModel):
    """
    Captures a single validation finding detected while analyzing an import batch.

    Issues can be raised at the file, column, row, or cell level and retain severity, code,
    human-readable messaging, the raw value that triggered the rule, and optional metadata for
    debugging/downloadable reports.
    """

    id = models.AutoField(primary_key=True, editable=False)

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="validation_issues",
    )

    severity = models.CharField(
        max_length=10,
        choices=ValidationSeverityChoices.choices,
    )
    code = models.CharField(
        max_length=50,
        choices=ValidationCodeChoices.choices,
    )

    message = models.TextField()
    help_text = models.TextField(blank=True)

    row_number = models.PositiveIntegerField(null=True, blank=True)
    column_name = models.CharField(max_length=255, blank=True)
    field_name = models.CharField(max_length=255, blank=True)

    raw_value = models.TextField(blank=True)
    normalized_value = models.TextField(blank=True)

    # Useful for structured debugging / download reports
    metadata = models.JSONField(default=dict, blank=True)

    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_import_issues",
    )

    class Meta:
        ordering = ["row_number", "column_name", "created_at"]
        indexes = [
            models.Index(fields=["import_batch", "severity"]),
            models.Index(fields=["import_batch", "code"]),
            models.Index(fields=["import_batch", "row_number"]),
        ]

    def __str__(self) -> str:
        row_part = f"Row {self.row_number}" if self.row_number else "File-level"
        return f"{row_part} - {self.code}"


# =========================================================
# Optional row corrections before import
# =========================================================
class ImportRowCorrection(TimeStampedModel):
    """
    Records a user-supplied correction for a specific row/column combination inside a batch.

    These corrections allow analysts to tweak data between validation runs, preserving both the
    before/after values and who made the adjustment for auditability.
    """

    id = models.AutoField(primary_key=True, editable=False)

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="row_corrections",
    )

    row_number = models.PositiveIntegerField()
    column_name = models.CharField(max_length=255)

    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)

    reason = models.TextField(blank=True)

    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="import_row_corrections",
    )

    class Meta:
        ordering = ["row_number", "column_name", "created_at"]
        indexes = [
            models.Index(fields=["import_batch", "row_number"]),
        ]

    def __str__(self) -> str:
        return f"Row {self.row_number} - {self.column_name}"


# =========================================================
# Background import execution
# =========================================================
class ImportJob(TimeStampedModel):
    """
    Tracks the asynchronous execution of importing a validated batch into Vision.

    The model keeps queue metadata, worker task identifiers, progress counters, last error details,
    and rollback timestamps so operators can monitor long-running jobs and resume/retry safely.
    """

    id = models.AutoField(primary_key=True, editable=False)

    import_batch = models.OneToOneField(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="import_job",
    )

    queued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="queued_import_jobs",
    )

    status = models.CharField(
        max_length=20,
        choices=ImportJobStatusChoices.choices,
        default=ImportJobStatusChoices.QUEUED,
    )

    task_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Celery/RQ/background worker task identifier",
    )

    progress_percent = models.PositiveSmallIntegerField(default=0)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    succeeded_rows = models.PositiveIntegerField(default=0)
    failed_rows = models.PositiveIntegerField(default=0)
    skipped_rows = models.PositiveIntegerField(default=0)

    retry_count = models.PositiveSmallIntegerField(default=0)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    last_error_code = models.CharField(max_length=100, blank=True)
    last_error_message = models.TextField(blank=True)

    rollback_started_at = models.DateTimeField(null=True, blank=True)
    rollback_completed_at = models.DateTimeField(null=True, blank=True)

    execution_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["started_at"]),
        ]

    def __str__(self) -> str:
        return f"ImportJob<{self.import_batch_id}> [{self.status}]"

    def clean(self):
        if self.progress_percent < 0 or self.progress_percent > 100:
            raise ValidationError({"progress_percent": "Progress must be between 0 and 100."})


# =========================================================
# Per-row import outcomes
# =========================================================
class ImportJobRowResult(TimeStampedModel):
    """
    Persists the per-row outcome generated by an import job.

    Row results allow the platform to show fine-grained success/failure feedback, keep the exact
    normalized payload that was sent to downstream models, and supply the data needed for partial
    rollbacks or targeted retries.
    """

    id = models.AutoField(primary_key=True, editable=False)

    job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="row_results",
    )

    row_number = models.PositiveIntegerField()
    action = models.CharField(max_length=20, choices=ImportRowActionChoices.choices)

    target_model = models.CharField(max_length=150, blank=True)
    target_object_pk = models.CharField(max_length=100, blank=True)

    status_message = models.TextField(blank=True)
    error_details = models.JSONField(default=dict, blank=True)

    row_payload = models.JSONField(default=dict, blank=True)
    normalized_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["row_number"]
        unique_together = [("job", "row_number")]
        indexes = [
            models.Index(fields=["job", "action"]),
            models.Index(fields=["job", "row_number"]),
        ]

    def __str__(self) -> str:
        return f"Job {self.job_id} - Row {self.row_number}"


# =========================================================
# Rollback tracking
# =========================================================
class ImportRollbackRecord(TimeStampedModel):
    """
    Describes a rollback operation executed against a completed import job.

    Stores who initiated the rollback, whether it succeeded, how many rows were reverted, and any
    additional metadata required to audit or debug the compensating action.
    """

    id = models.AutoField(primary_key=True, editable=False)

    job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="rollback_records",
    )

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="initiated_import_rollbacks",
        null=True,
        blank=True,
    )

    reason = models.TextField(blank=True)
    was_successful = models.BooleanField(default=False)

    reverted_rows_count = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"Rollback<{self.job_id}> success={self.was_successful}"


# =========================================================
# Import audit / history
# =========================================================
class ImportAuditLog(TimeStampedModel):
    """
    Import-specific audit trail that captures who performed which action and on what entity.

    Each log links back to the branch, batch, and optional job plus before/after/diff snapshots so
    operators can reconstruct the history of mapping edits, validations, imports, and rollbacks.
    """

    id = models.AutoField(primary_key=True, editable=False)

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="import_audit_logs",
    )

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )

    job = models.ForeignKey(
        ImportJob,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="import_audit_events",
        null=True,
        blank=True,
    )

    action = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100, blank=True)
    entity_id = models.CharField(max_length=100, blank=True)

    before_data = models.JSONField(default=dict, blank=True)
    after_data = models.JSONField(default=dict, blank=True)
    diff_data = models.JSONField(default=dict, blank=True)

    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "action"]),
            models.Index(fields=["import_batch"]),
            models.Index(fields=["job"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} @ {self.created_at}"


# =========================================================
# Completion / failure notification tracking
# =========================================================
class ImportNotification(TimeStampedModel):
    """
    Represents a notification that should be delivered to an internal user about an import event.

    The record links back to the batch, stores the intended recipient and templated content, and
    keeps delivery status/error details so reminders or retries can be coordinated later.
    """

    id = models.AutoField(primary_key=True, editable=False)

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="notifications",
    )

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="import_notifications",
    )

    event_type = models.CharField(
        max_length=50,
        help_text="Examples: validation_completed, import_succeeded, import_failed",
    )
    title = models.CharField(max_length=200)
    body = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=NotificationStatusChoices.choices,
        default=NotificationStatusChoices.PENDING,
    )

    sent_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["event_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} -> {self.recipient}"
