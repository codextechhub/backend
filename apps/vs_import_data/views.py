from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.response import success_response, error_response

from vs_rbac.permissions import IsAuthenticatedAndActive, IsBranchAdmin, IsSchoolAdmin, IsVisionStaff
from vs_schools.models import School
from vs_user.models import User

from .models import (
    ImportBatch,
    ImportJob,
    ImportNotification,
    ImportRollbackRecord,
    ImportRowCorrection,
    ImportTemplate,
    ImportValidationIssue,
    TemplateStatusChoices,
    FileFormatChoices,
)
from .serializers import (
    ImportBatchDetailSerializer,
    ImportBatchListSerializer,
    ImportBatchUpdateSerializer,
    ImportBatchUploadSerializer,
    ImportJobDetailSerializer,
    ImportJobListSerializer,
    ImportNotificationSerializer,
    ImportRollbackRecordSerializer,
    ImportRowCorrectionCreateSerializer,
    ImportRowCorrectionSerializer,
    ImportTemplateCreateSerializer,
    ImportTemplateDetailSerializer,
    ImportTemplateListSerializer,
    ImportValidationIssueDetailSerializer,
    ImportValidationIssueListSerializer,
    ImportValidationIssueResolveSerializer,
    RevalidateAfterCorrectionSerializer,
    RollbackImportSerializer,
    StartImportSerializer,
    ValidateImportBatchSerializer,
)
from .services.audit_service import create_import_audit_log
from .services.import_executor import execute_import
from .services.rollback_service import rollback_import_job
from .services.template_file import (
    generate_template_csv,
    generate_template_xlsx,
)
from .services.validation_service import validate_import_batch


# =========================================================
# Reusable mixins
# =========================================================
class SchoolContextMixin:
    """
    Gets school from URL and exposes it to serializers/views.
    URL must include: school_id
    """
    school_lookup_url_kwarg = "school_id"

    def get_school(self):
        from rest_framework.exceptions import PermissionDenied
        school_id = self.kwargs[self.school_lookup_url_kwarg]
        school = get_object_or_404(School, id=school_id)
        user = self.request.user
        if getattr(user, 'user_type', None) != User.UserType.CX_STAFF:
            if not user.school_id or str(user.school_id) != str(school_id):
                raise PermissionDenied("You do not have access to this school.")
        return school

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["school"] = self.get_school()
        return context


class ImportBatchContextMixin(SchoolContextMixin):
    """
    Gets an import batch belonging to the current school.
    URL must include: batch_id
    """
    batch_lookup_url_kwarg = "batch_id"

    def get_import_batch(self):
        return get_object_or_404(
            ImportBatch.objects.select_related(
                "school",
                "uploaded_by",
                "template",
            ).prefetch_related(
                "template__columns",
            ),
            id=self.kwargs[self.batch_lookup_url_kwarg],
            school=self.get_school(),
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["import_batch"] = getattr(self, "_cached_import_batch", None) or self.get_import_batch()
        return context


class ImportJobContextMixin(ImportBatchContextMixin):
    """
    Gets an import job belonging to the current import batch.
    URL must include: job_id
    """
    job_lookup_url_kwarg = "job_id"

    def get_job(self):
        return get_object_or_404(
            ImportJob.objects.select_related(
                "import_batch",
                "queued_by",
            ),
            id=self.kwargs[self.job_lookup_url_kwarg],
            import_batch=self.get_import_batch(),
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["job"] = getattr(self, "_cached_job", None) or self.get_job()
        return context


# =========================================================
# System Import Template Views
# =========================================================
class SystemImportTemplateListView(generics.ListCreateAPIView):
    """
    GET  -> list available official system templates (all authenticated staff).
    POST -> create a new system template with columns (CX_STAFF only).
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportTemplateCreateSerializer
        return ImportTemplateListSerializer

    def get_queryset(self):
        queryset = ImportTemplate.objects.prefetch_related("columns").order_by("dataset_type", "name")

        if self.request.method == "GET":
            queryset = queryset.filter(
                status=TemplateStatusChoices.ACTIVE,
                is_download_enabled=True,
            )

        dataset_type = self.request.query_params.get("dataset_type")
        if dataset_type:
            queryset = queryset.filter(dataset_type=dataset_type)

        return queryset

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method == "POST" and getattr(request.user, "user_type", None) != User.UserType.CX_STAFF:
            self.permission_denied(request, message="Only CX staff can create import templates.")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = serializer.save()
        create_import_audit_log(
            action="template_created",
            actor=request.user,
            entity_type="ImportTemplate",
            entity_id=str(template.id),
            after_data={"code": template.code, "name": template.name, "dataset_type": template.dataset_type},
            message=f"Import template '{template.name}' created.",
        )
        out = ImportTemplateDetailSerializer(template, context=self.get_serializer_context())
        return Response(
            {"status": "success", "message": "Template created.", "data": out.data},
            status=status.HTTP_201_CREATED,
        )


class SystemImportTemplateDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """
    GET -> retrieve one official system template.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportTemplateDetailSerializer
    lookup_url_kwarg = "template_id"

    def get_queryset(self):
        return ImportTemplate.objects.filter(
            status=TemplateStatusChoices.ACTIVE,
            is_download_enabled=True,
        ).prefetch_related("columns")


class SystemImportTemplateDownloadView(APIView):
    """
    GET -> download a template file as CSV or XLSX.

    Query param:
        ?format=csv
        ?format=xlsx
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def get(self, request, template_id):
        template = get_object_or_404(
            ImportTemplate.objects.prefetch_related("columns"),
            id=template_id,
            status=TemplateStatusChoices.ACTIVE,
            is_download_enabled=True,
        )

        requested_format = request.query_params.get("format", template.default_file_format)
        filename_base = f"{template.code}_v{template.version}"

        if requested_format == FileFormatChoices.CSV:
            content = generate_template_csv(template)
            response = HttpResponse(content, content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="{filename_base}.csv"'
            return response

        content = generate_template_xlsx(template)
        response = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename_base}.xlsx"'
        return response


# =========================================================
# Import Batch Views
# =========================================================
class ImportBatchListCreateView(CreateModelMixin, SchoolContextMixin, generics.ListCreateAPIView):
    """
    GET  -> list import batches for an school
    POST -> upload a new import batch using a selected system template
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def get_queryset(self):
        queryset = (
            ImportBatch.objects.filter(school=self.get_school())
            .select_related("school", "uploaded_by", "template")
            .order_by("-created_at")
        )

        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)

        template_id = self.request.query_params.get("template_id")
        if template_id:
            queryset = queryset.filter(template_id=template_id)

        return queryset

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportBatchUploadSerializer
        return ImportBatchListSerializer

    def perform_create(self, serializer):
        serializer.save()
        instance = serializer.instance
        create_import_audit_log(
            school=instance.school,
            branch=instance.branch,
            action="batch_uploaded",
            actor=self.request.user,
            import_batch=instance,
            entity_type="ImportBatch",
            entity_id=str(instance.id),
            after_data={"original_filename": instance.original_filename, "dataset_type": instance.dataset_type},
            message=f"Import batch '{instance.original_filename}' uploaded.",
        )


class ImportBatchDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, ImportBatchContextMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    -> full import batch details
    PATCH  -> update simple metadata only
    DELETE -> delete batch
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def get_queryset(self):
        return (
            ImportBatch.objects.filter(school=self.get_school())
            .select_related("school", "uploaded_by", "template")
            .prefetch_related(
                "template__columns",
                "validation_issues",
                "row_corrections",
                "notifications",
            )
        )

    def get_object(self):
        self._cached_import_batch = super().get_object()
        return self._cached_import_batch

    def get_serializer_class(self):
        if self.request.method in ["PATCH", "PUT"]:
            return ImportBatchUpdateSerializer
        return ImportBatchDetailSerializer

    def perform_update(self, serializer):
        before = {f: getattr(serializer.instance, f, None) for f in serializer.validated_data}
        serializer.save()
        instance = serializer.instance
        create_import_audit_log(
            school=instance.school,
            branch=instance.branch,
            action="batch_updated",
            actor=self.request.user,
            import_batch=instance,
            entity_type="ImportBatch",
            entity_id=str(instance.id),
            before_data=before,
            after_data=dict(serializer.validated_data),
            message="Import batch metadata updated.",
        )

    def perform_destroy(self, instance):
        create_import_audit_log(
            school=instance.school,
            branch=instance.branch,
            action="batch_deleted",
            actor=self.request.user,
            import_batch=instance,
            entity_type="ImportBatch",
            entity_id=str(instance.id),
            before_data={"original_filename": instance.original_filename, "status": instance.status},
            message=f"Import batch '{instance.original_filename}' deleted.",
        )
        instance.delete()


# =========================================================
# Validation Views
# =========================================================
class ValidateImportBatchView(ImportBatchContextMixin, APIView):
    """
    POST -> validate an import batch against its selected template.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def post(self, request, **_kwargs):
        import_batch = self.get_import_batch()

        serializer = ValidateImportBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = validate_import_batch(import_batch)

        create_import_audit_log(
            school=import_batch.school,
            branch=import_batch.branch,
            action="batch_validated",
            actor=request.user,
            import_batch=import_batch,
            entity_type="ImportBatch",
            entity_id=str(import_batch.id),
            after_data=result["summary"],
            message="Import batch validation completed.",
        )

        return success_response(
            message="Validation completed successfully.",
            data={"summary": result["summary"]},
        )


class ImportValidationIssueListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    GET -> list validation issues for a batch.
    Optional query params:
        ?severity=error
        ?is_resolved=true
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportValidationIssueListSerializer

    def get_queryset(self):
        queryset = ImportValidationIssue.objects.filter(
            import_batch=self.get_import_batch()
        ).order_by("row_number", "created_at")

        severity = self.request.query_params.get("severity")
        if severity:
            queryset = queryset.filter(severity=severity)

        is_resolved = self.request.query_params.get("is_resolved")
        if is_resolved is not None:
            normalized = is_resolved.lower()
            if normalized in {"true", "1"}:
                queryset = queryset.filter(is_resolved=True)
            elif normalized in {"false", "0"}:
                queryset = queryset.filter(is_resolved=False)

        return queryset


class ImportValidationIssueDetailView(RetrieveModelMixin, ImportBatchContextMixin, generics.RetrieveAPIView):
    """
    GET -> retrieve one validation issue.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportValidationIssueDetailSerializer
    lookup_url_kwarg = "issue_id"

    def get_queryset(self):
        return ImportValidationIssue.objects.filter(import_batch=self.get_import_batch())


class ResolveImportValidationIssueView(UpdateModelMixin, ImportBatchContextMixin, generics.UpdateAPIView):
    """
    PATCH -> mark a validation issue as resolved.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportValidationIssueResolveSerializer
    lookup_url_kwarg = "issue_id"

    def get_queryset(self):
        return ImportValidationIssue.objects.filter(import_batch=self.get_import_batch())

    def perform_update(self, serializer):
        serializer.save()
        issue = serializer.instance
        import_batch = issue.import_batch
        create_import_audit_log(
            school=import_batch.school,
            branch=import_batch.branch,
            action="issue_resolved",
            actor=self.request.user,
            import_batch=import_batch,
            entity_type="ImportValidationIssue",
            entity_id=str(issue.id),
            before_data={"is_resolved": False},
            after_data={"is_resolved": True},
            message=f"Validation issue '{issue.code}' marked as resolved.",
        )


# =========================================================
# Row Correction Views
# =========================================================
class ImportRowCorrectionListCreateView(CreateModelMixin, ImportBatchContextMixin, generics.ListCreateAPIView):
    """
    GET  -> list row corrections
    POST -> create a row correction
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def get_queryset(self):
        return (
            ImportRowCorrection.objects.filter(import_batch=self.get_import_batch())
            .select_related("corrected_by")
            .order_by("row_number", "created_at")
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportRowCorrectionCreateSerializer
        return ImportRowCorrectionSerializer

    def perform_create(self, serializer):
        serializer.save()
        correction = serializer.instance
        import_batch = correction.import_batch
        create_import_audit_log(
            school=import_batch.school,
            branch=import_batch.branch,
            action="row_correction_created",
            actor=self.request.user,
            import_batch=import_batch,
            entity_type="ImportRowCorrection",
            entity_id=str(correction.id),
            after_data={
                "row_number": correction.row_number,
                "column_name": correction.column_name,
                "old_value": correction.old_value,
                "new_value": correction.new_value,
            },
            message=f"Row correction applied to row {correction.row_number}, column '{correction.column_name}'.",
        )


class RevalidateAfterCorrectionView(ImportBatchContextMixin, APIView):
    """
    POST -> re-run validation after corrections.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def post(self, request, **_kwargs):
        import_batch = self.get_import_batch()

        serializer = RevalidateAfterCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = validate_import_batch(import_batch)

        create_import_audit_log(
            school=import_batch.school,
            branch=import_batch.branch,
            action="batch_revalidated",
            actor=request.user,
            import_batch=import_batch,
            entity_type="ImportBatch",
            entity_id=str(import_batch.id),
            after_data=result["summary"],
            message="Import batch revalidated after corrections.",
        )

        return success_response(
            message="Revalidation completed successfully.",
            data={"summary": result["summary"]},
        )


# =========================================================
# Import Job Views
# =========================================================
class StartImportBatchView(ImportBatchContextMixin, APIView):
    """
    POST -> start actual import execution.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def post(self, request, **_kwargs):
        import_batch = self.get_import_batch()

        serializer = StartImportSerializer(
            data=request.data,
            context={"import_batch": import_batch},
        )
        serializer.is_valid(raise_exception=True)

        create_import_audit_log(
            school=import_batch.school,
            branch=import_batch.branch,
            action="import_triggered",
            actor=request.user,
            import_batch=import_batch,
            entity_type="ImportBatch",
            entity_id=str(import_batch.id),
            message="Import execution triggered.",
        )

        job = execute_import(import_batch=import_batch, queued_by=request.user)

        return success_response(
            message="Import started successfully.",
            data={"job_id": str(job.id), "job_status": job.status},
        )


class ImportJobListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    GET -> list jobs for one batch.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportJobListSerializer

    def get_queryset(self):
        return (
            ImportJob.objects.filter(import_batch=self.get_import_batch())
            .select_related("queued_by")
            .order_by("-created_at")
        )


class ImportJobDetailView(RetrieveModelMixin, ImportJobContextMixin, generics.RetrieveAPIView):
    """
    GET -> retrieve one import job with row results.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportJobDetailSerializer
    lookup_url_kwarg = "job_id"

    def get_queryset(self):
        return (
            ImportJob.objects.filter(import_batch=self.get_import_batch())
            .select_related("queued_by")
            .prefetch_related("row_results")
        )


class RollbackImportJobView(ImportJobContextMixin, APIView):
    """
    POST -> rollback an import job.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def post(self, request, **_kwargs):
        job = self.get_job()

        serializer = RollbackImportSerializer(
            data=request.data,
            context={"job": job},
        )
        serializer.is_valid(raise_exception=True)

        rollback_record = rollback_import_job(
            job=job,
            initiated_by=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )

        return success_response(
            message="Rollback completed successfully.",
            data={
                "rollback_id": str(rollback_record.id),
                "reverted_rows_count": rollback_record.reverted_rows_count,
            },
        )


class ImportRollbackRecordListView(ImportJobContextMixin, generics.ListAPIView):
    """
    GET -> list rollback history for one job.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportRollbackRecordSerializer

    def get_queryset(self):
        return ImportRollbackRecord.objects.filter(
            job=self.get_job()
        ).order_by("-started_at")


# =========================================================
# Audit / Notification Views
# =========================================================
class ImportAuditLogListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    GET -> list AuditEvents for one import batch, scoped by batch pk in metadata.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]

    def get_serializer_class(self):
        from vs_audit.serializers import AuditEventListSerializer
        return AuditEventListSerializer

    def get_queryset(self):
        from vs_audit.models import AuditEvent, AuditModuleKey
        batch = self.get_import_batch()
        return (
            AuditEvent.objects.filter(
                module_key=AuditModuleKey.IMPORT,
                metadata__import_batch_id=str(batch.pk),
            )
            .select_related("actor_user")
            .order_by("-event_at")
        )


class ImportNotificationListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    GET -> list notifications for one import batch.
    """
    permission_classes = [IsAuthenticatedAndActive & (IsVisionStaff | IsSchoolAdmin | IsBranchAdmin)]
    serializer_class = ImportNotificationSerializer

    def get_queryset(self):
        return (
            ImportNotification.objects.filter(import_batch=self.get_import_batch())
            .select_related("recipient")
            .order_by("-created_at")
        )