from __future__ import annotations

import os

from django.utils import timezone
from rest_framework import serializers

from .models import (
    FileFormatChoices,
    ImportAuditLog,
    ImportBatch,
    ImportColumnMapping,
    ImportJob,
    ImportJobRowResult,
    ImportNotification,
    ImportRollbackRecord,
    ImportRowCorrection,
    ImportSourceChoices,
    ImportTemplate,
    ImportValidationIssue,
)


# =========================================================
# Small reusable helpers
# =========================================================
class UserMiniSerializer(serializers.Serializer):
    """
    Small reusable serializer for showing user info in nested responses.
    Use this only if you don't already have a shared user serializer.
    """
    id = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True, required=False)
    full_name = serializers.CharField(read_only=True, required=False)


class BranchMiniSerializer(serializers.Serializer):
    """
    Small reusable serializer for showing branch info in nested responses.
    Use this only if you don't already have a shared branch serializer.
    """
    id = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True, required=False)
    code = serializers.CharField(read_only=True, required=False)


# =========================================================
# Import Template Serializers
# =========================================================
class ImportTemplateListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for list pages.
    """
    class Meta:
        model = ImportTemplate
        fields = (
            "id",
            "name",
            "dataset_type",
            "description",
            "is_active",
            "last_used_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class ImportTemplateDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one template.
    """
    created_by = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()

    class Meta:
        model = ImportTemplate
        fields = (
            "id",
            "branch",
            "created_by",
            "name",
            "dataset_type",
            "description",
            "mapping_schema",
            "is_active",
            "last_used_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "branch",
            "created_by",
            "last_used_at",
            "created_at",
            "updated_at",
        )

    def get_created_by(self, obj):
        user = obj.created_by
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }

    def get_branch(self, obj):
        branch = obj.branch
        return {
            "id": str(branch.id),
            "name": getattr(branch, "name", ""),
            "code": getattr(branch, "code", ""),
        }


class ImportTemplateCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Used for creating and editing template records.
    Branch and created_by are taken from context/view, not user input.
    """

    class Meta:
        model = ImportTemplate
        fields = (
            "name",
            "dataset_type",
            "description",
            "mapping_schema",
            "is_active",
        )

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Template name cannot be empty.")
        return value

    def validate_mapping_schema(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("mapping_schema must be a JSON object/dictionary.")
        return value

    def create(self, validated_data):
        validated_data["branch"] = self.context["branch"]
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


# =========================================================
# Import Column Mapping Serializers
# =========================================================
class ImportColumnMappingSerializer(serializers.ModelSerializer):
    """
    Full mapping serializer.
    """

    class Meta:
        model = ImportColumnMapping
        fields = (
            "id",
            "import_batch",
            "template",
            "source_column",
            "target_field",
            "source",
            "confidence_score",
            "is_required",
            "is_confirmed",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "created_at",
            "updated_at",
        )


class ImportColumnMappingCreateSerializer(serializers.ModelSerializer):
    """
    Create/update mapping rows for one import batch.
    import_batch should usually come from the URL/view.
    """

    class Meta:
        model = ImportColumnMapping
        fields = (
            "template",
            "source_column",
            "target_field",
            "source",
            "confidence_score",
            "is_required",
            "is_confirmed",
        )

    def validate_source_column(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("source_column cannot be empty.")
        return value

    def validate_target_field(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("target_field cannot be empty.")
        return value

    def create(self, validated_data):
        validated_data["import_batch"] = self.context["import_batch"]
        return super().create(validated_data)


# =========================================================
# Validation Issue Serializers
# =========================================================
class ImportValidationIssueListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for validation results table/list.
    """

    class Meta:
        model = ImportValidationIssue
        fields = (
            "id",
            "severity",
            "code",
            "message",
            "row_number",
            "column_name",
            "field_name",
            "is_resolved",
            "created_at",
        )
        read_only_fields = fields


class ImportValidationIssueDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one validation issue.
    """
    resolved_by = serializers.SerializerMethodField()

    class Meta:
        model = ImportValidationIssue
        fields = (
            "id",
            "import_batch",
            "severity",
            "code",
            "message",
            "help_text",
            "row_number",
            "column_name",
            "field_name",
            "raw_value",
            "normalized_value",
            "metadata",
            "is_resolved",
            "resolved_at",
            "resolved_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_resolved_by(self, obj):
        if not obj.resolved_by:
            return None
        user = obj.resolved_by
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


class ImportValidationIssueResolveSerializer(serializers.ModelSerializer):
    """
    Used when marking an issue as resolved.
    """

    class Meta:
        model = ImportValidationIssue
        fields = ("is_resolved",)

    def validate_is_resolved(self, value):
        if value is not True:
            raise serializers.ValidationError("This action only supports setting is_resolved to true.")
        return value

    def update(self, instance, validated_data):
        instance.is_resolved = True
        instance.resolved_at = timezone.now()
        instance.resolved_by = self.context["request"].user
        instance.save(update_fields=["is_resolved", "resolved_at", "resolved_by", "updated_at"])
        return instance


# =========================================================
# Row Correction Serializers
# =========================================================
class ImportRowCorrectionSerializer(serializers.ModelSerializer):
    """
    Full serializer for row corrections.
    """
    corrected_by = serializers.SerializerMethodField()

    class Meta:
        model = ImportRowCorrection
        fields = (
            "id",
            "import_batch",
            "row_number",
            "column_name",
            "old_value",
            "new_value",
            "reason",
            "corrected_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "import_batch",
            "corrected_by",
            "created_at",
            "updated_at",
        )

    def get_corrected_by(self, obj):
        user = obj.corrected_by
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


class ImportRowCorrectionCreateSerializer(serializers.ModelSerializer):
    """
    Create a manual correction for a specific row/column.
    """

    class Meta:
        model = ImportRowCorrection
        fields = (
            "row_number",
            "column_name",
            "old_value",
            "new_value",
            "reason",
        )

    def validate_row_number(self, value):
        if value <= 0:
            raise serializers.ValidationError("row_number must be greater than 0.")
        return value

    def validate_column_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("column_name cannot be empty.")
        return value

    def create(self, validated_data):
        validated_data["import_batch"] = self.context["import_batch"]
        validated_data["corrected_by"] = self.context["request"].user
        return super().create(validated_data)


# =========================================================
# Import Job Row Result Serializers
# =========================================================
class ImportJobRowResultSerializer(serializers.ModelSerializer):
    """
    Shows the result of each processed row during import execution.
    """

    class Meta:
        model = ImportJobRowResult
        fields = (
            "id",
            "job",
            "row_number",
            "action",
            "target_model",
            "target_object_pk",
            "status_message",
            "error_details",
            "row_payload",
            "normalized_payload",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# =========================================================
# Import Job Serializers
# =========================================================
class ImportJobListSerializer(serializers.ModelSerializer):
    """
    Simple serializer for import job listing.
    """

    class Meta:
        model = ImportJob
        fields = (
            "id",
            "import_batch",
            "status",
            "progress_percent",
            "total_rows",
            "processed_rows",
            "succeeded_rows",
            "failed_rows",
            "skipped_rows",
            "retry_count",
            "started_at",
            "completed_at",
            "created_at",
        )
        read_only_fields = fields


class ImportJobDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one import job.
    """
    queued_by = serializers.SerializerMethodField()
    row_results = ImportJobRowResultSerializer(many=True, read_only=True)

    class Meta:
        model = ImportJob
        fields = (
            "id",
            "import_batch",
            "queued_by",
            "status",
            "task_id",
            "progress_percent",
            "total_rows",
            "processed_rows",
            "succeeded_rows",
            "failed_rows",
            "skipped_rows",
            "retry_count",
            "started_at",
            "completed_at",
            "last_error_code",
            "last_error_message",
            "rollback_started_at",
            "rollback_completed_at",
            "execution_summary",
            "row_results",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_queued_by(self, obj):
        user = obj.queued_by
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


# =========================================================
# Rollback Serializers
# =========================================================
class ImportRollbackRecordSerializer(serializers.ModelSerializer):
    """
    Serializer for rollback history.
    """
    initiated_by = serializers.SerializerMethodField()

    class Meta:
        model = ImportRollbackRecord
        fields = (
            "id",
            "job",
            "initiated_by",
            "reason",
            "was_successful",
            "reverted_rows_count",
            "details",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_initiated_by(self, obj):
        if not obj.initiated_by:
            return None
        user = obj.initiated_by
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


# =========================================================
# Audit Log Serializers
# =========================================================
class ImportAuditLogSerializer(serializers.ModelSerializer):
    """
    Serializer for import audit/history records.
    """
    actor = serializers.SerializerMethodField()

    class Meta:
        model = ImportAuditLog
        fields = (
            "id",
            "branch",
            "import_batch",
            "job",
            "actor",
            "action",
            "entity_type",
            "entity_id",
            "before_data",
            "after_data",
            "diff_data",
            "message",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_actor(self, obj):
        if not obj.actor:
            return None
        user = obj.actor
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


# =========================================================
# Notification Serializers
# =========================================================
class ImportNotificationSerializer(serializers.ModelSerializer):
    """
    Serializer for import notifications.
    """
    recipient = serializers.SerializerMethodField()

    class Meta:
        model = ImportNotification
        fields = (
            "id",
            "import_batch",
            "recipient",
            "event_type",
            "title",
            "body",
            "status",
            "sent_at",
            "error_message",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_recipient(self, obj):
        user = obj.recipient
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


# =========================================================
# Import Batch Serializers
# =========================================================
class ImportBatchListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for import batch list page.
    Includes counts useful on dashboard/table view.
    """
    error_count = serializers.IntegerField(read_only=True)
    warning_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ImportBatch
        fields = (
            "id",
            "original_filename",
            "file_format",
            "dataset_type",
            "detected_dataset_type",
            "detection_confidence",
            "status",
            "file_size_bytes",
            "total_rows",
            "total_columns",
            "has_critical_errors",
            "is_ready_for_import",
            "error_count",
            "warning_count",
            "imported_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class ImportBatchDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one import batch.
    Includes nested child records for easy API consumption.
    """
    branch = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()

    column_mappings = ImportColumnMappingSerializer(many=True, read_only=True)
    validation_issues = ImportValidationIssueListSerializer(many=True, read_only=True)
    row_corrections = ImportRowCorrectionSerializer(many=True, read_only=True)
    notifications = ImportNotificationSerializer(many=True, read_only=True)

    error_count = serializers.IntegerField(read_only=True)
    warning_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ImportBatch
        fields = (
            "id",
            "branch",
            "uploaded_by",
            "source",
            "original_filename",
            "file",
            "file_format",
            "dataset_type",
            "detected_dataset_type",
            "detection_confidence",
            "status",
            "file_size_bytes",
            "total_rows",
            "total_columns",
            "header_row_index",
            "sheet_name",
            "detected_columns",
            "preview_rows",
            "validation_summary",
            "has_critical_errors",
            "is_ready_for_import",
            "validation_started_at",
            "validation_completed_at",
            "imported_at",
            "notes",
            "error_count",
            "warning_count",
            "column_mappings",
            "validation_issues",
            "row_corrections",
            "notifications",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_branch(self, obj):
        branch = obj.branch
        return {
            "id": str(branch.id),
            "name": getattr(branch, "name", ""),
            "code": getattr(branch, "code", ""),
        }

    def get_uploaded_by(self, obj):
        user = obj.uploaded_by
        return {
            "id": str(user.id),
            "email": getattr(user, "email", ""),
            "full_name": getattr(user, "full_name", ""),
        }


class ImportBatchUploadSerializer(serializers.ModelSerializer):
    """
    Used when uploading a new import file.
    branch and uploaded_by should come from the view/context.
    """
    file = serializers.FileField(write_only=True)

    class Meta:
        model = ImportBatch
        fields = (
            "source",
            "file",
            "dataset_type",
            "sheet_name",
            "header_row_index",
            "notes",
        )

    def validate_source(self, value):
        allowed = {
            ImportSourceChoices.DIRECT_UPLOAD,
            ImportSourceChoices.SECURE_LINK,
            ImportSourceChoices.SYSTEM_GENERATED,
        }
        if value not in allowed:
            raise serializers.ValidationError("Invalid source.")
        return value

    def validate_header_row_index(self, value):
        if value <= 0:
            raise serializers.ValidationError("header_row_index must be greater than 0.")
        return value

    def validate_file(self, value):
        """
        Validate uploaded file extension.
        Real deep file-content validation should still happen in service layer.
        """
        name = value.name.lower()
        ext = os.path.splitext(name)[1]

        allowed_extensions = {".csv", ".xlsx", ".xls"}
        if ext not in allowed_extensions:
            raise serializers.ValidationError("Only .csv, .xlsx, and .xls files are allowed.")

        return value

    def create(self, validated_data):
        uploaded_file = validated_data.pop("file")

        filename = uploaded_file.name.lower()
        ext = os.path.splitext(filename)[1]

        if ext == ".csv":
            file_format = FileFormatChoices.CSV
        elif ext == ".xlsx":
            file_format = FileFormatChoices.XLSX
        else:
            file_format = FileFormatChoices.XLS

        validated_data["branch"] = self.context["branch"]
        validated_data["uploaded_by"] = self.context["request"].user
        validated_data["file"] = uploaded_file
        validated_data["original_filename"] = uploaded_file.name
        validated_data["file_format"] = file_format
        validated_data["file_size_bytes"] = uploaded_file.size

        return super().create(validated_data)


class ImportBatchUpdateSerializer(serializers.ModelSerializer):
    """
    Used for simple metadata edits on an import batch.
    Usually not for validation/import execution itself.
    """

    class Meta:
        model = ImportBatch
        fields = (
            "dataset_type",
            "sheet_name",
            "header_row_index",
            "notes",
        )

    def validate_header_row_index(self, value):
        if value <= 0:
            raise serializers.ValidationError("header_row_index must be greater than 0.")
        return value


# =========================================================
# Workflow / Action Serializers
# =========================================================
class ApplyTemplateSerializer(serializers.Serializer):
    """
    Input serializer for applying a saved template to an import batch.
    """
    template_id = serializers.UUIDField()


class AutoMapSerializer(serializers.Serializer):
    """
    Input serializer for triggering automatic field mapping.
    """
    overwrite_existing = serializers.BooleanField(default=False)


class BulkColumnMappingItemSerializer(serializers.Serializer):
    """
    One mapping row used in bulk mapping submission.
    """
    source_column = serializers.CharField()
    target_field = serializers.CharField()
    source = serializers.CharField(default="manual")
    confidence_score = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    is_required = serializers.BooleanField(default=False)
    is_confirmed = serializers.BooleanField(default=True)

    def validate_source_column(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("source_column cannot be empty.")
        return value

    def validate_target_field(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("target_field cannot be empty.")
        return value


class BulkColumnMappingSerializer(serializers.Serializer):
    """
    Input serializer for submitting many mappings at once.
    """
    mappings = BulkColumnMappingItemSerializer(many=True)

    def validate_mappings(self, value):
        if not value:
            raise serializers.ValidationError("At least one mapping is required.")

        seen_source_columns = set()
        for item in value:
            source_column = item["source_column"].strip().lower()
            if source_column in seen_source_columns:
                raise serializers.ValidationError(
                    f"Duplicate source_column found: {item['source_column']}"
                )
            seen_source_columns.add(source_column)

        return value


class ValidateImportBatchSerializer(serializers.Serializer):
    """
    Input serializer for validation action.
    """
    run_full_validation = serializers.BooleanField(default=True)
    include_warnings = serializers.BooleanField(default=True)


class StartImportSerializer(serializers.Serializer):
    """
    Input serializer for starting import execution.
    """
    run_async = serializers.BooleanField(default=True)
    stop_on_first_error = serializers.BooleanField(default=False)

    def validate(self, attrs):
        import_batch = self.context.get("import_batch")
        if import_batch and not import_batch.is_ready_for_import:
            raise serializers.ValidationError(
                "This import batch is not ready for import."
            )
        return attrs


class RollbackImportSerializer(serializers.Serializer):
    """
    Input serializer for rolling back an import.
    """
    reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        job = self.context.get("job")
        if job and job.status not in {"failed", "cancelled", "succeeded"}:
            raise serializers.ValidationError(
                "Only completed, failed, or cancelled jobs can be rolled back."
            )
        return attrs


class RevalidateAfterCorrectionSerializer(serializers.Serializer):
    """
    Input serializer for revalidation after making row corrections.
    """
    clear_resolved_flags = serializers.BooleanField(default=False)