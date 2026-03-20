from __future__ import annotations

import os
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from vs_institutions.models import Institution


# =========================================================
# Base model
# =========================================================
class TimeStampedModel(models.Model):
    """Reusable timestamps."""
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# =========================================================
# Choices / constants
# =========================================================
class ImportSourceChoices(models.TextChoices):
    DIRECT_UPLOAD = "direct_upload", "Direct Upload"
    SECURE_LINK = "secure_link", "Secure Intake Link"
    SYSTEM_GENERATED = "system_generated", "System Generated"


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


# =========================================================
# Upload path helper
# =========================================================
def import_file_upload_to(instance: "ImportBatch", filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    institution_slug = getattr(instance.institution, "slug", "institution")
    return (
        f"imports/{institution_slug}/{instance.dataset_type or 'unknown'}/"
        f"{instance.id}{ext}"
    )


# =========================================================
# Main import batch
# =========================================================
class ImportBatch(TimeStampedModel):
    """
    One uploaded file and its full lifecycle:
    upload -> detect -> map -> validate -> import -> history.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="import_batches",
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_import_batches",
    )

    source = models.CharField(
        max_length=30,
        choices=ImportSourceChoices.choices,
        default=ImportSourceChoices.DIRECT_UPLOAD,
    )

    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to=import_file_upload_to)
    file_format = models.CharField(max_length=10, choices=FileFormatChoices.choices)

    # What the user selected vs what the system detected
    dataset_type = models.CharField(
        max_length=30,
        choices=DatasetTypeChoices.choices,
        default=DatasetTypeChoices.GENERIC,
    )
    detected_dataset_type = models.CharField(
        max_length=30,
        choices=DatasetTypeChoices.choices,
        blank=True,
    )
    detection_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Stored as percent-like score, e.g. 84.50",
    )

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
    detected_columns = models.JSONField(default=list, blank=True)
    preview_rows = models.JSONField(default=list, blank=True)
    validation_summary = models.JSONField(default=dict, blank=True)

    has_critical_errors = models.BooleanField(default=False)
    is_ready_for_import = models.BooleanField(default=False)

    validation_started_at = models.DateTimeField(null=True, blank=True)
    validation_completed_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["institution", "dataset_type"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.institution} - {self.dataset_type} - {self.original_filename}"

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


# =========================================================
# Reusable import templates
# =========================================================
class ImportTemplate(TimeStampedModel):
    """
    A reusable mapping template for a dataset type within an institution.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="import_templates",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_import_templates",
    )

    name = models.CharField(max_length=150)
    dataset_type = models.CharField(max_length=30, choices=DatasetTypeChoices.choices)

    description = models.TextField(blank=True)

    # Example:
    # {
    #   "Student Name": {"target_field": "full_name", "required": true},
    #   "DOB": {"target_field": "date_of_birth", "required": false}
    # }
    mapping_schema = models.JSONField(default=dict)

    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("institution", "name", "dataset_type")]
        indexes = [
            models.Index(fields=["institution", "dataset_type", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.dataset_type})"


# =========================================================
# Column-level mapping for a batch
# =========================================================
class ImportColumnMapping(TimeStampedModel):
    """
    Maps one uploaded column to one Vision target field.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="column_mappings",
    )

    template = models.ForeignKey(
        ImportTemplate,
        on_delete=models.SET_NULL,
        related_name="applied_mappings",
        null=True,
        blank=True,
    )

    source_column = models.CharField(max_length=255)
    target_field = models.CharField(max_length=255)

    source = models.CharField(
        max_length=20,
        choices=MappingSourceChoices.choices,
        default=MappingSourceChoices.AUTO,
    )
    confidence_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )

    is_required = models.BooleanField(default=False)
    is_confirmed = models.BooleanField(default=False)

    class Meta:
        ordering = ["source_column"]
        unique_together = [("import_batch", "source_column")]
        indexes = [
            models.Index(fields=["import_batch", "target_field"]),
        ]

    def __str__(self) -> str:
        return f"{self.source_column} -> {self.target_field}"


# =========================================================
# Validation results
# =========================================================
class ImportValidationIssue(TimeStampedModel):
    """
    One validation issue found in a file.
    Can be file-level, column-level, row-level, or cell-level.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
    Stores a manual fix made against a row before a revalidation/import.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
    Async execution record for importing an already validated batch.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
    Stores the outcome of each processed row during execution.
    Useful for partial failure reports and rollback tracing.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
    Tracks rollback attempts for failed or cancelled jobs.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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
    Lightweight import-specific audit trail.
    You may later merge this into your central audit module if preferred.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    institution = models.ForeignKey(
        Institution,
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
            models.Index(fields=["institution", "action"]),
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
    Tracks admin notifications about validation/import completion.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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