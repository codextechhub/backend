from __future__ import annotations

import os
import re

from django.utils import timezone
from rest_framework import serializers
from vs_rbac.fls import FieldSecurityMixin

from .constants import ImportPermission

from .models import (
    FileFormatChoices,
    ImportBatch,
    ImportJob,
    ImportJobRowResult,
    ImportJobStatusChoices,
    ImportNotification,
    ImportRollbackRecord,
    ImportTemplate,
    ImportTemplateColumn,
    ImportValidationIssue,
    TemplateStatusChoices,
)


# =========================================================
# Small reusable nested display helpers
# =========================================================
class UserMiniSerializer(serializers.Serializer):
    """
    Small serializer for nested user display.
    """
    id = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True, required=False)
    full_name = serializers.CharField(read_only=True, required=False)


class SchoolMiniSerializer(serializers.Serializer):
    """
    Small serializer for nested school display.
    """
    id = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True, required=False)
    slug = serializers.CharField(read_only=True, required=False)


# =========================================================
# Import Template Column Serializers
# =========================================================
class ImportTemplateColumnListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for template column listings.
    """

    class Meta:
        model = ImportTemplateColumn
        fields = (
            "id",
            "column_name",
            "target_field",
            "data_type",
            "is_required",
            "is_unique",
            "column_order",
        )
        read_only_fields = fields


class ImportTemplateColumnDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one template column.
    """

    class Meta:
        model = ImportTemplateColumn
        fields = (
            "id",
            "column_name",
            "target_field",
            "display_name",
            "help_text",
            "data_type",
            "is_required",
            "is_unique",
            "max_length",
            "allowed_values",
            "sample_value",
            "default_value",
            "column_order",
            "reference_model",
            "reference_lookup_field",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# =========================================================
# Import Template Serializers
# =========================================================
class ImportTemplateListSerializer(serializers.ModelSerializer):
    """
    Used to list available system templates for download/use.
    """
    total_columns = serializers.IntegerField(source="columns.count", read_only=True)

    class Meta:
        model = ImportTemplate
        fields = (
            "id",
            "code",
            "name",
            "dataset_type",
            "description",
            "status",
            "default_file_format",
            "is_download_enabled",
            "total_columns",
            "published_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class ImportTemplateDetailSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """
    Full template detail serializer.
    Includes all column definitions.
    """
    read_permissions = {
        "validation_rules": ImportPermission.TEMPLATE_MANAGE,
    }

    columns = ImportTemplateColumnDetailSerializer(many=True, read_only=True)

    class Meta:
        model = ImportTemplate
        fields = (
            "id",
            "code",
            "name",
            "dataset_type",
            "description",
            "status",
            "default_file_format",
            "instructions",
            "allow_sample_row",
            "sample_row_data",
            "validation_rules",
            "is_download_enabled",
            "published_at",
            "retired_at",
            "columns",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# =========================================================
# Import Template Write Serializers (create / update)
# =========================================================
class ImportTemplateColumnWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportTemplateColumn
        fields = (
            "column_name",
            "target_field",
            "display_name",
            "help_text",
            "data_type",
            "is_required",
            "is_unique",
            "max_length",
            "allowed_values",
            "sample_value",
            "default_value",
            "column_order",
            "reference_model",
            "reference_lookup_field",
        )


class ImportTemplateCreateSerializer(serializers.ModelSerializer):
    columns = ImportTemplateColumnWriteSerializer(many=True, required=False, default=list)

    class Meta:
        model = ImportTemplate
        fields = (
            "code",
            "name",
            "dataset_type",
            "description",
            "status",
            "default_file_format",
            "instructions",
            "allow_sample_row",
            "sample_row_data",
            "validation_rules",
            "is_download_enabled",
            "columns",
        )

    def create(self, validated_data):
        columns_data = validated_data.pop("columns", [])
        validated_data["created_by"] = self.context["request"].user

        template = ImportTemplate.objects.create(**validated_data)

        if columns_data:
            ImportTemplateColumn.objects.bulk_create([
                ImportTemplateColumn(template=template, **col)
                for col in columns_data
            ])

        return template


class ImportTemplateUpdateSerializer(serializers.ModelSerializer):
    """
    Used when a CX staff member PATCHes an existing system template.
    Columns are optional; when provided they fully replace all existing columns.
    dataset_type and code are intentionally excluded — change them via migration.
    """
    columns = ImportTemplateColumnWriteSerializer(many=True, required=False)

    class Meta:
        model = ImportTemplate
        fields = (
            "name",
            "description",
            "status",
            "default_file_format",
            "instructions",
            "allow_sample_row",
            "sample_row_data",
            "validation_rules",
            "is_download_enabled",
            "columns",
        )

    def update(self, instance, validated_data):
        columns_data = validated_data.pop("columns", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if validated_data.get("status") == TemplateStatusChoices.ACTIVE and not instance.published_at:
            instance.published_at = timezone.now()
        if validated_data.get("status") == TemplateStatusChoices.RETIRED and not instance.retired_at:
            instance.retired_at = timezone.now()

        instance.save()

        if columns_data is not None:
            instance.columns.all().delete()
            ImportTemplateColumn.objects.bulk_create([
                ImportTemplateColumn(template=instance, **col)
                for col in columns_data
            ])

        return instance


# =========================================================
# Import Validation Issue Serializers
# =========================================================
class ImportValidationIssueListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for validation issue tables/lists.
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
    Used for marking an issue as resolved.
    """

    class Meta:
        model = ImportValidationIssue
        fields = ("is_resolved",)

    def validate_is_resolved(self, value):
        if value is not True:
            raise serializers.ValidationError("This action only supports setting is_resolved to true.")
        return value

    def update(self, instance, _validated_data):
        instance.is_resolved = True
        instance.resolved_at = timezone.now()
        instance.resolved_by = self.context["request"].user
        instance.save(update_fields=["is_resolved", "resolved_at", "resolved_by", "updated_at"])
        return instance



# =========================================================
# Import Job Row Result Serializers
# =========================================================
class ImportJobRowResultSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """
    Shows one processed row result from an import job.
    """
    read_permissions = {
        "row_payload": ImportPermission.JOB_VIEW,
        "normalized_payload": ImportPermission.JOB_VIEW,
        "error_details": ImportPermission.JOB_VIEW,
    }

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
    Lightweight serializer for job listing.
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


class ImportJobDetailSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """
    Full serializer for one import job.
    """
    read_permissions = {
        "execution_summary": ImportPermission.JOB_VIEW,
        "last_error_code": ImportPermission.JOB_VIEW,
        "last_error_message": ImportPermission.JOB_VIEW,
    }

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
    Serializer for rollback history records.
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
    Lightweight serializer for import batch listing.
    """
    error_count = serializers.IntegerField(read_only=True)
    warning_count = serializers.IntegerField(read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True, default=None)
    template_code = serializers.CharField(source="template.code", read_only=True, default=None)

    class Meta:
        model = ImportBatch
        fields = (
            "id",
            "template",
            "template_name",
            "template_code",
            "original_filename",
            "file_format",
            "status",
            "file_size_bytes",
            "total_rows",
            "total_columns",
            "structure_matches_template",
            "has_critical_errors",
            "is_ready_for_import",
            "error_count",
            "warning_count",
            "imported_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class ImportBatchDetailSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """
    Full serializer for one import batch.
    """
    read_permissions = {
        "file": ImportPermission.BATCH_VIEW,
        "preview_rows": ImportPermission.BATCH_VIEW,
    }

    school = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()
    template = ImportTemplateDetailSerializer(read_only=True)

    validation_issues = ImportValidationIssueListSerializer(many=True, read_only=True)
    notifications = ImportNotificationSerializer(many=True, read_only=True)

    error_count = serializers.IntegerField(read_only=True)
    warning_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ImportBatch
        fields = (
            "id",
            "school",
            "branch",
            "uploaded_by",
            "template",
            "original_filename",
            "file",
            "file_format",
            "dataset_type",
            "status",
            "file_size_bytes",
            "total_rows",
            "total_columns",
            "header_row_index",
            "sheet_name",
            "uploaded_headers",
            "template_headers_snapshot",
            "preview_rows",
            "validation_summary",
            "structure_matches_template",
            "has_critical_errors",
            "is_ready_for_import",
            "validation_started_at",
            "validation_completed_at",
            "imported_at",
            "notes",
            "error_count",
            "warning_count",
            "validation_issues",
            "notifications",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_school(self, obj):
        school = obj.school
        if not school:
            return None
        return {
            "id": str(school.id),
            "name": getattr(school, "name", ""),
            "slug": getattr(school, "slug", ""),
        }

    def get_branch(self, obj):
        branch = obj.branch
        if not branch:
            return None
        return {
            "id": str(branch.id),
            "name": getattr(branch, "name", ""),
            "slug": getattr(branch, "slug", ""),
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
    Used when uploading a new import batch.

    In the new system-template-only flow:
    - template is required
    - file is required
    - school and uploaded_by come from context
    """
    file = serializers.FileField(write_only=True)
    template_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = ImportBatch
        fields = (
            "template_id",
            "file",
            "sheet_name",
            "header_row_index",
            "notes",
        )

    def validate_header_row_index(self, value):
        if value <= 0:
            raise serializers.ValidationError("header_row_index must be greater than 0.")
        return value

    def validate_file(self, value):
        """
        Basic file extension and size check only.
        Deep content validation should happen in service layer.
        """
        if value.size > 50 * 1024 * 1024:
            raise serializers.ValidationError("File exceeds the 50 MB limit.")

        name = value.name.lower()
        ext = os.path.splitext(name)[1]

        allowed_extensions = {".csv", ".xlsx", ".xls"}
        if ext not in allowed_extensions:
            raise serializers.ValidationError("Only .csv, .xlsx, and .xls files are allowed.")

        return value

    def validate_template_id(self, value):
        try:
            template = ImportTemplate.objects.prefetch_related("columns").get(id=value)
        except ImportTemplate.DoesNotExist:
            raise serializers.ValidationError("Selected template does not exist.")

        if not template.is_download_enabled:
            raise serializers.ValidationError("This template is not available for use.")

        self._validated_template = template
        return value

    def validate(self, attrs):
        template = getattr(self, "_validated_template", None)
        uploaded_file = attrs.get("file")
        if template and uploaded_file:
            ext = os.path.splitext(uploaded_file.name.lower())[1].lstrip(".")
            expected = template.default_file_format.lower()
            if ext != expected:
                raise serializers.ValidationError({
                    "file": (
                        f"This template only accepts {expected.upper()} files. "
                        f"You uploaded a {ext.upper()} file. Please convert your file to {expected.upper()} and try again."
                    )
                })
        return attrs

    def create(self, validated_data):
        from .services.file_parser import parse_import_file

        uploaded_file = validated_data.pop("file")
        validated_data.pop("template_id")

        filename = uploaded_file.name.lower()
        ext = os.path.splitext(filename)[1]

        if ext == ".csv":
            file_format = FileFormatChoices.CSV
        elif ext == ".xlsx":
            file_format = FileFormatChoices.XLSX
        else:
            file_format = FileFormatChoices.XLS

        template = getattr(self, "_validated_template", None)
        if template is None:
            raise serializers.ValidationError({"template_id": "Template could not be resolved."})

        template_headers_snapshot = list(
            template.columns.order_by("column_order").values_list("column_name", flat=True)
        )

        safe_name = os.path.basename(uploaded_file.name)
        if not re.fullmatch(r"[A-Za-z0-9_.\- ]+", safe_name):
            raise serializers.ValidationError({"file": "Filename contains invalid characters. Use only letters, numbers, spaces, hyphens, underscores, and dots."})

        sheet_name = validated_data.get("sheet_name")
        header_row_index = validated_data.get("header_row_index") or 1
        try:
            detected_headers, preview_rows = parse_import_file(
                uploaded_file,
                file_format=file_format,
                sheet_name=sheet_name,
                header_row_index=header_row_index,
            )
        except ValueError as exc:
            raise serializers.ValidationError({"file": str(exc)})
        except Exception as exc:
            raise serializers.ValidationError({
                "file": f"Could not read file: {exc}. Ensure the file is not corrupted and matches the selected format.",
            })

        validated_data["tenant"] = self.context["request"].tenant
        validated_data["branch"] = self.context.get("branch")
        validated_data["uploaded_by"] = self.context["request"].user
        validated_data["template"] = template
        validated_data["dataset_type"] = template.dataset_type
        validated_data["file"] = uploaded_file
        validated_data["original_filename"] = safe_name
        validated_data["file_format"] = file_format
        validated_data["file_size_bytes"] = uploaded_file.size
        validated_data["template_headers_snapshot"] = template_headers_snapshot
        validated_data["uploaded_headers"] = detected_headers
        validated_data["preview_rows"] = preview_rows
        validated_data["total_rows"] = len(preview_rows)
        validated_data["total_columns"] = len(detected_headers)

        try:
            return super().create(validated_data)
        except OSError as exc:
            raise serializers.ValidationError({
                "file": f"File could not be saved on the server: {exc}. Contact support if this persists.",
            })
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("ImportBatch create failed")
            raise serializers.ValidationError({
                "non_field_errors": [f"Upload failed: {exc}"],
            })


class ImportBatchUpdateSerializer(serializers.ModelSerializer):
    """
    Used for simple metadata edits on a batch.
    Does not allow changing template after upload.
    """

    class Meta:
        model = ImportBatch
        fields = (
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
        if not import_batch:
            return attrs

        if not import_batch.is_ready_for_import:
            raise serializers.ValidationError("This import batch is not ready for import.")

        if ImportJob.objects.filter(
            import_batch=import_batch,
            status__in=[ImportJobStatusChoices.RUNNING, ImportJobStatusChoices.QUEUED],
        ).exists():
            raise serializers.ValidationError("An import job is already running for this batch.")

        if ImportJob.objects.filter(
            import_batch=import_batch,
            status=ImportJobStatusChoices.SUCCEEDED,
        ).exists():
            raise serializers.ValidationError(
                "This batch has already been imported successfully. Re-importing is not allowed."
            )

        return attrs


class RollbackImportSerializer(serializers.Serializer):
    """
    Input serializer for rollback action.
    """
    reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        job = self.context.get("job")
        if job and job.status not in {"failed", "cancelled", "succeeded"}:
            raise serializers.ValidationError(
                "Only completed, failed, or cancelled jobs can be rolled back."
            )
        return attrs

