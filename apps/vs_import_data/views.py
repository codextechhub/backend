from __future__ import annotations

import mimetypes
import os

from django.http import FileResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.response import success_response, error_response

from vs_rbac.permissions import IsAuthenticatedAndActive, IsBranchAdmin, IsSchoolAdmin, IsVisionStaff, HasRBACPermission
from vs_schools.models import School
from vs_user.models import User

from .constants import ImportPermission

from .models import (
    ImportBatch,
    ImportJob,
    ImportJobStatusChoices,
    ImportNotification,
    ImportRollbackRecord,
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
    ImportTemplateCreateSerializer,
    ImportTemplateDetailSerializer,
    ImportTemplateListSerializer,
    ImportTemplateUpdateSerializer,
    ImportValidationIssueDetailSerializer,
    ImportValidationIssueListSerializer,
    ImportValidationIssueResolveSerializer,
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
    generate_validation_issues_csv,
)
from .services.validation_service import validate_import_batch


# =========================================================
# Helpers
# =========================================================
def _format_validation_issues(issues: list[dict]) -> list[dict]:
    """
    Return a flat, sorted list of validation issues ready for API responses.
    File-level issues (no row) come first, then row issues sorted by row number.
    """
    return sorted(
        [
            {
                "severity":  issue.get("severity", "error"),
                "code":      issue.get("code", ""),
                "row":       issue.get("row_number"),
                "column":    issue.get("column_name") or None,
                "message":   issue.get("message", ""),
                "raw_value": issue.get("raw_value"),
                "help_text": issue.get("help_text") or "",
            }
            for issue in issues
        ],
        key=lambda i: (i["row"] is not None, i["row"] or 0, i["column"] or ""),
    )


# =========================================================
# Reusable mixins
# =========================================================
class SchoolContextMixin:
    """
    Resolves the school for the current request without requiring school_id in the URL.

    - Non-CX_STAFF: school is always the user's own school.
    - CX_STAFF: school is read from the optional ?school_id= query param (None = all schools).
    """

    def get_school(self):
        user = self.request.user
        if getattr(user, "user_type", None) == User.UserType.CX_STAFF:
            school_id = self.request.query_params.get("school_id")
            if school_id:
                return get_object_or_404(School, id=school_id)
            return None
        return get_object_or_404(School, id=user.school_id)

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
        filters = {"id": self.kwargs[self.batch_lookup_url_kwarg]}
        school = self.get_school()
        if school is not None:
            filters["school"] = school
        return get_object_or_404(
            ImportBatch.objects.select_related(
                "school",
                "uploaded_by",
                "template",
            ).prefetch_related(
                "template__columns",
            ),
            **filters,
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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def get_permissions(self):
        self.rbac_permission = (
            ImportPermission.TEMPLATE_CREATE
            if self.request.method == "POST"
            else ImportPermission.TEMPLATE_VIEW
        )
        return super().get_permissions()

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportTemplateCreateSerializer
        return ImportTemplateListSerializer

    def get_queryset(self):
        queryset = ImportTemplate.objects.prefetch_related("columns").order_by("dataset_type", "name")

        is_cx_staff = getattr(self.request.user, "user_type", None) == User.UserType.CX_STAFF
        if self.request.method == "GET" and not is_cx_staff:
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


class SystemImportTemplateDetailView(RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    GET   -> retrieve one official system template.
    PATCH -> update template metadata and/or columns (CX_STAFF + TEMPLATE_MANAGE only).
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    lookup_url_kwarg = "template_id"

    def get_permissions(self):
        self.rbac_permission = (
            ImportPermission.TEMPLATE_MANAGE
            if self.request.method in ("PATCH", "PUT")
            else ImportPermission.TEMPLATE_VIEW
        )
        return super().get_permissions()

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method in ("PATCH", "PUT") and getattr(request.user, "user_type", None) != User.UserType.CX_STAFF:
            self.permission_denied(request, message="Only CX staff can update import templates.")

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return ImportTemplateUpdateSerializer
        return ImportTemplateDetailSerializer

    def get_queryset(self):
        qs = ImportTemplate.objects.prefetch_related("columns")
        is_cx_staff = getattr(self.request.user, "user_type", None) == User.UserType.CX_STAFF
        if not is_cx_staff:
            qs = qs.filter(status=TemplateStatusChoices.ACTIVE, is_download_enabled=True)
        return qs

    def perform_update(self, serializer):
        before = {
            "name": serializer.instance.name,
            "status": serializer.instance.status,
            "description": serializer.instance.description,
        }
        template = serializer.save()
        create_import_audit_log(
            action="template_updated",
            actor=self.request.user,
            entity_type="ImportTemplate",
            entity_id=str(template.id),
            before_data=before,
            after_data={"name": template.name, "status": template.status},
            message=f"Import template '{template.name}' updated.",
        )


class SystemImportTemplateDownloadView(APIView):
    """
    GET -> download a template file as CSV or XLSX.

    Query param:
        ?file_format=csv
        ?file_format=xlsx
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.TEMPLATE_VIEW

    def get(self, request, template_id):
        qs = ImportTemplate.objects.prefetch_related("columns")
        is_cx_staff = getattr(request.user, "user_type", None) == User.UserType.CX_STAFF
        if not is_cx_staff:
            qs = qs.filter(status=TemplateStatusChoices.ACTIVE, is_download_enabled=True)
        template = get_object_or_404(qs, id=template_id)

        requested_format = request.query_params.get("file_format", template.default_file_format)
        filename_base = f"{template.code}_template"

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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def get_permissions(self):
        self.rbac_permission = (
            ImportPermission.BATCH_CREATE
            if self.request.method == "POST"
            else ImportPermission.BATCH_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        school = self.get_school()
        queryset = ImportBatch.objects.select_related("school", "uploaded_by", "template").order_by("-created_at")
        if school is not None:
            queryset = queryset.filter(school=school)

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

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        response_serializer = ImportBatchListSerializer(serializer.instance)
        return success_response(
            message="Import batch uploaded successfully.",
            data=response_serializer.data,
            status=status.HTTP_201_CREATED,
        )

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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    lookup_url_kwarg = "batch_id"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = ImportPermission.BATCH_DELETE
        elif self.request.method in ("PATCH", "PUT"):
            self.rbac_permission = ImportPermission.BATCH_UPDATE
        else:
            self.rbac_permission = ImportPermission.BATCH_VIEW
        return super().get_permissions()

    def get_queryset(self):
        school = self.get_school()
        qs = ImportBatch.objects.select_related("school", "uploaded_by", "template").prefetch_related(
            "template__columns",
            "validation_issues",
            "notifications",
        )
        if school is not None:
            qs = qs.filter(school=school)
        return qs

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
# Batch File Download
# =========================================================
class ImportBatchFileDownloadView(ImportBatchContextMixin, APIView):
    """
    GET -> stream the uploaded batch file as an attachment.

    Works in both DEBUG and non-DEBUG environments because it reads
    the file from MEDIA_ROOT and serves it directly rather than
    redirecting to a media URL.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.BATCH_VIEW

    def get(self, request, **_kwargs):
        school = self.get_school()
        qs = ImportBatch.objects.only("id", "school", "file", "original_filename")
        if school is not None:
            qs = qs.filter(school=school)
        batch = get_object_or_404(qs, id=_kwargs["batch_id"])

        if not batch.file:
            raise Http404("No file attached to this batch.")

        file_path = batch.file.path
        if not os.path.exists(file_path):
            raise Http404("File not found on server.")

        content_type, _ = mimetypes.guess_type(file_path)
        content_type = content_type or "application/octet-stream"

        response = FileResponse(
            open(file_path, "rb"),
            content_type=content_type,
            as_attachment=True,
            filename=batch.original_filename,
        )
        return response


# =========================================================
# Validation Views
# =========================================================
class ValidateImportBatchView(ImportBatchContextMixin, APIView):
    """
    POST -> validate an import batch against its selected template.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.BATCH_VALIDATE

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
            data={
                "summary": result["summary"],
                "issues": _format_validation_issues(result["issues"]),
            },
        )


class ImportValidationIssueListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    GET -> list validation issues for a batch.
    Optional query params:
        ?severity=error
        ?is_resolved=true
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.VALIDATION_VIEW
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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.VALIDATION_VIEW
    serializer_class = ImportValidationIssueDetailSerializer
    lookup_url_kwarg = "issue_id"

    def get_queryset(self):
        return ImportValidationIssue.objects.filter(import_batch=self.get_import_batch())


class ResolveImportValidationIssueView(UpdateModelMixin, ImportBatchContextMixin, generics.UpdateAPIView):
    """
    PATCH -> mark a validation issue as resolved.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.VALIDATION_RESOLVE
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


class ImportValidationIssueExportView(ImportBatchContextMixin, APIView):
    """
    GET -> download all validation issues for a batch as a CSV file.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.VALIDATION_VIEW

    def get(self, request, **_kwargs):
        import_batch = self.get_import_batch()
        content = generate_validation_issues_csv(import_batch)
        filename = f"validation_issues_batch_{import_batch.id}.csv"
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


# =========================================================
# Import Job Views
# =========================================================
class StartImportBatchView(ImportBatchContextMixin, APIView):
    """
    POST -> start actual import execution.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.BATCH_IMPORT

    def post(self, request, **_kwargs):
        from .tasks import execute_import_batch_task

        import_batch = self.get_import_batch()

        serializer = StartImportSerializer(
            data=request.data,
            context={"import_batch": import_batch},
        )
        serializer.is_valid(raise_exception=True)

        run_async = serializer.validated_data.get("run_async", True)

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

        if run_async:
            try:
                execute_import_batch_task.delay(
                    import_batch_id=str(import_batch.id),
                    queued_by_id=str(request.user.id),
                    _job_owner_id=str(request.user.id),
                    _job_school_id=import_batch.school_id,
                    _job_label=f"Import: {import_batch.original_filename or import_batch.dataset_type}",
                    _job_kind="import",
                )
            except Exception as exc:
                # With CELERY_TASK_ALWAYS_EAGER + CELERY_TASK_EAGER_PROPAGATES,
                # task failures propagate here. Return the real error so the
                # frontend can display it rather than a misleading broker message.
                import logging
                logging.getLogger(__name__).exception(
                    "Import task failed synchronously for batch %s", import_batch.id
                )
                return error_response(
                    message=f"Import failed: {exc}",
                    code="IMPORT_TASK_FAILED",
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return success_response(
                message="Import queued. Poll GET /batches/{id}/jobs/ for progress.",
                data={"batch_id": str(import_batch.id), "job_status": ImportJobStatusChoices.QUEUED},
            )

        try:
            job = execute_import(import_batch=import_batch, queued_by=request.user)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception(
                "Synchronous import failed for batch %s", import_batch.id
            )
            return error_response(
                message=f"Import failed: {exc}",
                code="IMPORT_TASK_FAILED",
                status=status.HTTP_400_BAD_REQUEST,
            )
        return success_response(
            message="Import completed.",
            data={"job_id": str(job.id), "job_status": job.status},
        )


class ImportJobListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    GET -> list jobs for one batch.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.JOB_VIEW
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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.JOB_VIEW
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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.ROLLBACK_RUN

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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.ROLLBACK_VIEW
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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.AUDIT_VIEW

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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = ImportPermission.NOTIFICATION_VIEW
    serializer_class = ImportNotificationSerializer

    def get_queryset(self):
        return (
            ImportNotification.objects.filter(import_batch=self.get_import_batch())
            .select_related("recipient")
            .order_by("-created_at")
        )