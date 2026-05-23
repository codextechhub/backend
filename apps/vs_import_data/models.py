from __future__ import annotations

import os
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from vs_schools.models import Branch, School


# =========================================================
# Base model
# =========================================================
class TimeStampedModel(models.Model):
    """
    Shared timestamp mixin covering creation and last-update times.

    This keeps auditing consistent across all import-related tables without repeating the
    column declarations on every model. Django handles the default/auto_now behaviors so
    derived models only need to inherit.

    Fields:
        created_at: Timezone-aware timestamp recorded when the row is inserted.
        updated_at: Auto-updated timestamp refreshed on each save.

    Meta:
        - declared as `abstract=True` so no standalone table is created.
    """
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
    SCHOOLS = "schools", "Schools"
    BRANCHES = "branches", "Branches"


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
    if instance.school_id:
        scope = getattr(instance.school, "slug", "school")
    elif instance.branch_id:
        scope = f"branch_{getattr(instance.branch, 'slug', instance.branch_id)}"
    else:
        scope = "internal"

    if instance.original_filename:
        base = os.path.splitext(instance.original_filename)[0]
        stamp = timezone.now().strftime("%Y%m%d_%H%M")
        stored_name = f"{base}_{stamp}{ext}"
    else:
        stored_name = f"{uuid.uuid4().hex}{ext}"
    return f"imports/{scope}/{instance.dataset_type or 'unknown'}/{stored_name}"

def import_template_file_upload_to(instance: "ImportTemplate", filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return f"system_import_templates/{instance.dataset_type}/{instance.code}{ext}"


# =========================================================
# Main import batch
# =========================================================
class ImportBatch(TimeStampedModel):
    """
    Represents a single uploaded dataset file as it moves through the full import
    pipeline: upload → detection → mapping → validation → execution. Every stage
    of that lifecycle is reflected on this record, making it the central tracking
    object for Module 9.

    A batch may be scoped to a School, a Branch, or neither (internal system use).
    Exactly one of school/branch may be set, or both may be null. It may optionally be
    linked to a system ImportTemplate. The file is stored using a scope-derived path
    via `import_file_upload_to`.

    Fields:
        school: Optional FK to School; set when the import is school-scoped.
        branch: Optional FK to Branch; set when the import is branch-scoped.
        uploaded_by: FK to the user who performed the upload (PROTECT on delete).
        template: Optional FK to ImportTemplate; the official system template selected
                  for this batch. Null when no template is explicitly chosen.
        file: The uploaded file stored at a branch/dataset-type/id-scoped path.
        status: Current pipeline stage (ImportBatchStatusChoices, default UPLOADED).
        file_size_bytes: Raw byte size of the uploaded file, defaulting to 0.
        total_rows: Row count parsed from the file (excluding header), defaulting to 0.
        total_columns: Column count parsed from the file, defaulting to 0.
        header_row_index: Position of the header row in the file (1-based, default 1).
        sheet_name: Optional sheet identifier for Excel files with multiple sheets.
        notes: Free-text operator notes attached to the batch.
        uploaded_headers: JSON list of column headers exactly as they appear in the
                          uploaded file, captured at parse time.
        template_headers_snapshot: JSON list of the expected headers from the linked
                                   ImportTemplate at the moment of upload, used for
                                   structural comparison.
        structure_matches_template: Boolean flag indicating whether the uploaded file's
                                    headers align with the template snapshot.
        has_critical_errors: True if any ERROR-severity validation issues exist for
                             this batch; gates the import trigger.
        is_ready_for_import: True when validation has passed and the batch is cleared
                             for background execution.
        validation_started_at: Timestamp recorded when the validation pass begins.
        validation_completed_at: Timestamp recorded when the validation pass finishes.
        imported_at: Timestamp recorded when the ImportJob completes successfully.

    Meta:
        - ordering newest-first by created_at.
        - indexes on (school, status), (school, dataset_type), and created_at
          to serve dashboard and filter queries efficiently.
    """

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="import_batches",
        null=True,
        blank=True,
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="import_batches",
        null=True,
        blank=True,
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

    dataset_type = models.CharField(
        max_length=30,
        choices=DatasetTypeChoices.choices,
        blank=True,
    )

    file = models.FileField(upload_to=import_file_upload_to)
    file_format = models.CharField(
        max_length=10,
        choices=FileFormatChoices.choices,
        blank=True,
    )
    original_filename = models.CharField(max_length=255, blank=True)

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
    preview_rows = models.JSONField(default=list, blank=True)  # First N parsed rows, stored for validation service use
    structure_matches_template = models.BooleanField(default=False)  # Indicates if the uploaded file structure matches the template
    validation_summary = models.JSONField(null=True, blank=True)  # Summary dict written after validation completes

    has_critical_errors = models.BooleanField(default=False)
    is_ready_for_import = models.BooleanField(default=False)

    validation_started_at = models.DateTimeField(null=True, blank=True)
    validation_completed_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["school", "status"]),
            models.Index(fields=["school", "dataset_type"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        scope = self.school or self.branch or "internal"
        return f"{scope} - {self.dataset_type} - {self.original_filename}"

    def clean(self):
        allowed = {FileFormatChoices.CSV, FileFormatChoices.XLSX, FileFormatChoices.XLS}
        if self.file_format and self.file_format not in allowed:
            raise ValidationError({"file_format": "Only CSV and Excel files are supported."})

    @property
    def error_count(self) -> int:
        return self.validation_issues.filter(severity=ValidationSeverityChoices.ERROR).count()

    @property
    def warning_count(self) -> int:
        return self.validation_issues.filter(severity=ValidationSeverityChoices.WARNING).count()


class ImportTemplate(TimeStampedModel):
    """
    System-managed definition of the canonical column structure for a given dataset
    type. Templates are the source of truth that admins download before filling data
    and that the import pipeline uses to validate uploaded files.

    Templates are versioned and follow a lifecycle (draft → active → retired). Only
    one template per dataset type is expected to be active at a time. Column
    specifications are stored on related ImportTemplateColumn records.

    Fields:
        code: Stable internal identifier, unique across all templates.
              Example: "students_master_v1". Used for programmatic lookups.
        name: Human-readable display label for the template.
        dataset_type: The kind of data this template governs (DatasetTypeChoices).
        description: Optional narrative summary shown to admins in the UI.
        version: Semantic version string (default "1.0"); incremented on structural
                 column changes.
        status: Publication state (TemplateStatusChoices, default ACTIVE).
        default_file_format: Preferred format for generated download files
                             (FileFormatChoices, default CSV).
        template_file: Optional pre-generated downloadable file stored at a
                       dataset-type/code-scoped path.
        instructions: Plain-language guidance displayed to admins before they
                      download or upload against this template.
        allow_sample_row: Whether a sample data row is included in generated files.
        sample_row_data: JSON object representing the optional example row injected
                         into generated template files.
        validation_rules: JSON object holding optional dataset-wide validation config
                          consumed by the validator layer.
        is_download_enabled: Controls whether the template is currently available for
                             admin download.
        created_by: Optional FK to the user who created the template record.
        published_at: Timestamp when the template was promoted to active status.
        retired_at: Timestamp when the template was retired and superseded.

    Meta:
        - ordering by dataset_type, name, then descending version.
        - indexes on (dataset_type, status) for active-template lookups and on code
          for direct programmatic access.
    """

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
        default=FileFormatChoices.CSV,
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
    A single column specification within an ImportTemplate. Each record maps a
    spreadsheet header to an internal target field and declares the data type,
    validation rules, display hints, and optional cross-reference metadata needed
    by the import engine and template generator.

    Fields:
        template: FK to the owning ImportTemplate (CASCADE on delete).
        column_name: Exact header string expected in the uploaded file, used for
                     structural matching. Example: "Date of Birth".
        target_field: Internal field identifier the import handler maps this column
                      to. Example: "date_of_birth".
        display_name: Optional friendly label used in documentation or UI if the
                      column_name is not human-readable enough.
        help_text: Explanation of what the admin should enter in this column, shown
                   in generated template files or UI tooltips.
        data_type: Expected data type for values in this column
                   (TemplateColumnDataTypeChoices, default STRING).
        is_required: Whether a missing value in this column constitutes a validation
                     error.
        is_unique: Whether duplicate values in this column across rows are flagged
                   as errors.
        max_length: Optional upper bound on string length for string-typed columns.
        allowed_values: JSON list of permitted values; active when data_type is
                        CHOICE or a fixed-value constraint applies.
        sample_value: Example value injected into the generated template file so
                      admins understand the expected format.
        default_value: Fallback value applied during import when the cell is blank,
                       if validation rules permit it.
        column_order: Integer used to sort columns in generated files and UI displays.
        reference_model: Optional name of the domain model this column cross-references
                         during validation. Example: "Staff", "Campus".
        reference_lookup_field: The field on the reference model used to resolve the
                                cross-reference. Example: "full_name", "code".

    Meta:
        - ordering by column_order then column_name.
        - unique_together constraints on (template, column_name) and
          (template, target_field) to prevent duplicate mappings within a template.
        - indexes on (template, column_order) and (template, is_required) to
          support ordered rendering and required-column filtering.
    """

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

    sample_value = models.CharField(max_length=255, blank=True)  # For generating example rows in the template file
    default_value = models.CharField(max_length=255, blank=True)  # For filling in missing values during import if allowed by validation rules

    column_order = models.PositiveIntegerField(default=1)  # For ordering columns in the generated template file and UI display

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

    def clean(self):
        super().clean()
        if not isinstance(self.allowed_values, list):
            raise ValidationError({'allowed_values': 'allowed_values must be a JSON array.'})
        if not all(isinstance(v, str) for v in self.allowed_values):
            raise ValidationError({'allowed_values': 'Every item in allowed_values must be a string.'})

    def __str__(self) -> str:
        return f"{self.template.code} - {self.column_name}"


# =========================================================
# Validation results
# =========================================================
class ImportValidationIssue(TimeStampedModel):
    """
    An individual result emitted during the validation pass of an ImportBatch.
    One record is written per issue found, whether file-level or row-level. The
    aggregate of these records determines whether a batch can proceed to import.

    Fields:
        import_batch: FK to the batch being validated (CASCADE on delete).
        severity: Urgency level of the issue (ValidationSeverityChoices:
                  ERROR, WARNING, INFO). ERRORs block import progression.
        code: Machine-readable issue code (ValidationCodeChoices) used for
              grouping, filtering, and export diagnostics.
        message: Human-readable description of the specific issue surfaced in the UI.
        help_text: Optional supplementary guidance explaining how to correct the issue.
        row_number: Spreadsheet row index (1-based) where the issue was found.
                   Null for file-level issues such as missing sheets or wrong format.
        column_name: Header name of the column where the issue was detected. Blank
                     for row-level or file-level issues not tied to a specific column.
        field_name: Internal field name corresponding to column_name, if resolved.
        raw_value: The original cell value as read from the file before any
                   transformation, stored for diagnostics and download reports.
        normalized_value: The cleaned or coerced value after transformation, if any
                          processing was applied before the issue was raised.
        metadata: JSON object for structured debugging data such as reference mismatches,
                  allowed value lists, or rule violation details.
        is_resolved: True if an operator has manually acknowledged or resolved this issue.
        resolved_at: Timestamp when the issue was marked resolved.
        resolved_by: FK to the user who resolved the issue; SET_NULL on user deletion.

    Meta:
        - ordering by row_number, column_name, created_at.
        - indexes on (import_batch, severity), (import_batch, code), and
          (import_batch, row_number) for issue filtering and export queries.
    """

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
# Background import execution
# =========================================================
class ImportJob(TimeStampedModel):
    """
    Background execution record for a validated ImportBatch. Created when an admin
    confirms and queues the import. The Celery worker updates this record as it
    processes rows in chunks, recording throughput counters and rollback timestamps
    that operators and the UI use to monitor progress or diagnose failures.

    One batch maps to exactly one job (OneToOne). Rollback attempts are tracked
    separately on ImportRollbackRecord.

    Fields:
        import_batch: OneToOne FK to the ImportBatch being executed (CASCADE on delete).
        queued_by: FK to the user who triggered the import (PROTECT on delete).
        status: Current execution state (ImportJobStatusChoices, default QUEUED).
        task_id: Background worker task identifier (Celery task ID) stored for
                 status polling and cancellation.
        progress_percent: Integer 0–100 representing execution progress, updated
                          periodically by the worker.
        total_rows: Total row count the worker expects to process.
        processed_rows: Running count of rows the worker has attempted so far.
        succeeded_rows: Count of rows that resulted in successful CREATE or UPDATE.
        failed_rows: Count of rows that encountered an error and were not persisted.
        skipped_rows: Count of rows the worker intentionally bypassed (e.g., duplicates
                      or rows flagged SKIP during mapping).
        retry_count: Number of times the job has been retried after a transient failure.
        started_at: Timestamp when the worker first picked up and began executing the job.
        completed_at: Timestamp when the worker finished, regardless of outcome.
        last_error_code: Machine-readable code of the most recent failure encountered.
        last_error_message: Human-readable description of the most recent failure.
        rollback_started_at: Timestamp when a compensating rollback run was initiated.
        rollback_completed_at: Timestamp when the rollback run finished.
        execution_summary: JSON object containing metrics emitted by dataset-type
                           handlers at the end of execution.

    Meta:
        - ordering newest-first by created_at.
        - indexes on status and started_at for dashboard and monitoring queries.
    """

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
    Granular outcome for a single spreadsheet row processed by an ImportJob. One
    record is written per row during execution, capturing the action taken, the
    domain object touched, and both the raw and normalized data payloads. Enables
    row-level feedback in the UI, targeted retries, and full audit reconstruction.

    Fields:
        job: FK to the parent ImportJob (CASCADE on delete).
        row_number: Spreadsheet row index (1-based) identifying which row this result
                    corresponds to.
        action: Outcome category for this row (ImportRowActionChoices:
                CREATE, UPDATE, SKIP, FAILED).
        target_model: String name of the domain model that was created or updated.
                      Example: "Student", "Staff". Blank for SKIP and FAILED rows.
        target_object_pk: Primary key of the domain object that was created or updated,
                          stored as a string for cross-model flexibility.
        status_message: Human-readable description of what happened to this row,
                        used for UI display and export reports.
        error_details: JSON object with structured error context for FAILED rows,
                       used in support diagnostics and download reports.
        row_payload: JSON snapshot of the raw row data as parsed from the file,
                     before any normalization or transformation.
        normalized_payload: JSON snapshot of the row data after transformation and
                            field mapping, representing what was sent to the handler.

    Meta:
        - ordering by row_number.
        - unique_together on (job, row_number) to prevent duplicate result records
          for the same row within a job.
        - indexes on (job, action) and (job, row_number) for result filtering and
          per-row lookup.
    """

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
    Audit record for a rollback attempt applied to a completed ImportJob. Created
    whenever an operator initiates a reversal of a previously executed import.
    Captures the initiating user, the stated reason, outcome counters, and timing so
    compensating actions remain fully traceable.

    Fields:
        job: FK to the ImportJob being reversed (CASCADE on delete).
        initiated_by: FK to the user who triggered the rollback. Null if initiated
                      by automation; SET_NULL on user deletion.
        reason: Operator-supplied explanation for why the rollback was requested.
        was_successful: True if the rollback completed without errors.
        reverted_rows_count: Number of domain rows successfully reversed during this
                             rollback attempt.
        details: JSON object with structured rollback diagnostics, such as which
                 models were affected or partial failure notes.
        started_at: Timestamp when the rollback process began (default timezone.now).
        completed_at: Timestamp when the rollback process finished, null if still
                      in progress or interrupted.

    Meta:
        - ordering newest-first by started_at.
    """

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
# Completion / failure notification tracking
# =========================================================
class ImportNotification(TimeStampedModel):
    """
    Notification record targeting a specific user about a milestone or failure in an
    ImportBatch lifecycle. Created by the notification dispatch layer and updated as
    delivery succeeds or fails, enabling retry orchestration and delivery auditing.

    Fields:
        import_batch: FK to the ImportBatch that triggered this notification
                      (CASCADE on delete).
        recipient: FK to the user who should receive the message (CASCADE on delete).
        event_type: String key identifying the triggering milestone. Examples:
                    "validation_completed", "import_succeeded", "import_failed",
                    "rollback_completed".
        title: Short subject line rendered in the notification UI or email header.
        body: Full notification body text, may be plain text or HTML depending on
              the delivery channel.
        status: Current delivery state (NotificationStatusChoices:
                PENDING, SENT, FAILED).
        sent_at: Timestamp recorded when delivery was confirmed successful.
        error_message: Description of the failure reason if status is FAILED, used
                       for retry logic and support diagnostics.

    Meta:
        - ordering newest-first by created_at.
        - indexes on (recipient, status) for per-user inbox queries and on event_type
          for system-level delivery monitoring.
    """

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
