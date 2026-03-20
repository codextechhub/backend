from __future__ import annotations

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from vs_institutions.models import Institution

from .models import (
    ImportAuditLog,
    ImportBatch,
    ImportColumnMapping,
    ImportJob,
    ImportNotification,
    ImportRollbackRecord,
    ImportRowCorrection,
    ImportTemplate,
    ImportValidationIssue,
)
from .serializers import (
    ApplyTemplateSerializer,
    AutoMapSerializer,
    BulkColumnMappingSerializer,
    ImportAuditLogSerializer,
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
    ImportTemplateCreateUpdateSerializer,
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
from .services import (
    apply_template_to_batch,
    auto_map_columns,
    execute_import,
    rollback_import_job,
    save_bulk_mappings,
    validate_import_batch,
)


# =========================================================
# Simple placeholder permissions
# Replace with your real custom permissions later
# =========================================================
class IsAuthenticatedStaff(permissions.IsAuthenticated):
    """
    Basic placeholder permission.
    Replace with your real institution/module permission checks.
    """
    pass


# =========================================================
# Reusable mixins
# =========================================================
class InstitutionContextMixin:
    """
    Gets institution from URL and makes it available everywhere.
    URL must contain: institution_id
    """

    institution_lookup_url_kwarg = "institution_id"

    def get_institution(self):
        institution_id = self.kwargs[self.institution_lookup_url_kwarg]
        return get_object_or_404(Institution, id=institution_id)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["institution"] = self.get_institution()
        return context


class ImportBatchContextMixin(InstitutionContextMixin):
    """
    Gets an import batch belonging to the current institution.
    URL must contain: batch_id
    """

    batch_lookup_url_kwarg = "batch_id"

    def get_import_batch(self):
        return get_object_or_404(
            ImportBatch.objects.select_related("institution", "uploaded_by"),
            id=self.kwargs[self.batch_lookup_url_kwarg],
            institution=self.get_institution(),
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["import_batch"] = getattr(self, "_cached_import_batch", None) or self.get_import_batch()
        return context


class ImportJobContextMixin(ImportBatchContextMixin):
    """
    Gets an import job belonging to the current import batch.
    URL must contain: job_id
    """

    job_lookup_url_kwarg = "job_id"

    def get_job(self):
        return get_object_or_404(
            ImportJob.objects.select_related("import_batch", "queued_by"),
            id=self.kwargs[self.job_lookup_url_kwarg],
            import_batch=self.get_import_batch(),
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["job"] = getattr(self, "_cached_job", None) or self.get_job()
        return context


# =========================================================
# Import Template Views
# =========================================================
class ImportTemplateListCreateView(InstitutionContextMixin, generics.ListCreateAPIView):
    """
    GET  -> list templates for one institution
    POST -> create a new template
    """
    permission_classes = [IsAuthenticatedStaff]

    def get_queryset(self):
        return ImportTemplate.objects.filter(
            institution=self.get_institution()
        ).select_related("institution", "created_by").order_by("name")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportTemplateCreateUpdateSerializer
        return ImportTemplateListSerializer


class ImportTemplateDetailView(InstitutionContextMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    -> one template
    PATCH  -> update template
    DELETE -> delete template
    """
    permission_classes = [IsAuthenticatedStaff]
    lookup_url_kwarg = "template_id"

    def get_queryset(self):
        return ImportTemplate.objects.filter(
            institution=self.get_institution()
        ).select_related("institution", "created_by")

    def get_serializer_class(self):
        if self.request.method in ["PATCH", "PUT"]:
            return ImportTemplateCreateUpdateSerializer
        return ImportTemplateDetailSerializer


# =========================================================
# Import Batch Views
# =========================================================
class ImportBatchListCreateView(InstitutionContextMixin, generics.ListCreateAPIView):
    """
    GET  -> list import batches
    POST -> upload a new import batch
    """
    permission_classes = [IsAuthenticatedStaff]

    def get_queryset(self):
        return (
            ImportBatch.objects.filter(institution=self.get_institution())
            .select_related("institution", "uploaded_by")
            .prefetch_related("validation_issues")
            .order_by("-created_at")
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportBatchUploadSerializer
        return ImportBatchListSerializer


class ImportBatchDetailView(ImportBatchContextMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    -> full details of one import batch
    PATCH  -> edit simple batch metadata
    DELETE -> delete the batch
    """
    permission_classes = [IsAuthenticatedStaff]

    def get_queryset(self):
        return (
            ImportBatch.objects.filter(institution=self.get_institution())
            .select_related("institution", "uploaded_by")
            .prefetch_related(
                "column_mappings",
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


# =========================================================
# Mapping Views
# =========================================================
class ApplyTemplateToImportBatchView(ImportBatchContextMixin, APIView):
    """
    Apply a saved mapping template to an import batch.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()

        serializer = ApplyTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        template = get_object_or_404(
            ImportTemplate,
            id=serializer.validated_data["template_id"],
            institution=self.get_institution(),
        )

        mappings = apply_template_to_batch(import_batch=import_batch, template=template)

        return Response(
            {
                "message": "Template applied successfully.",
                "mappings_created": len(mappings),
            },
            status=status.HTTP_200_OK,
        )


class AutoMapImportBatchView(ImportBatchContextMixin, APIView):
    """
    Automatically map detected columns to target fields.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()

        serializer = AutoMapSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mappings = auto_map_columns(
            import_batch=import_batch,
            overwrite_existing=serializer.validated_data["overwrite_existing"],
        )

        return Response(
            {
                "message": "Auto-mapping completed.",
                "mappings_created": len(mappings),
            },
            status=status.HTTP_200_OK,
        )


class BulkColumnMappingView(ImportBatchContextMixin, APIView):
    """
    Save many mappings at once.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()

        serializer = BulkColumnMappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mappings = save_bulk_mappings(
            import_batch=import_batch,
            mappings=serializer.validated_data["mappings"],
            clear_existing=True,
        )

        return Response(
            {
                "message": "Mappings saved successfully.",
                "mappings_created": len(mappings),
            },
            status=status.HTTP_200_OK,
        )


class ImportBatchMappingsListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    List saved mappings for an import batch.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportBatchDetailSerializer

    def get(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()
        serializer = ImportBatchDetailSerializer(import_batch, context=self.get_serializer_context())
        return Response(
            {
                "batch_id": str(import_batch.id),
                "column_mappings": serializer.data["column_mappings"],
            }
        )


# =========================================================
# Validation Views
# =========================================================
class ValidateImportBatchView(ImportBatchContextMixin, APIView):
    """
    Run validation on the import batch.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()

        serializer = ValidateImportBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = validate_import_batch(import_batch)

        return Response(
            {
                "message": "Validation completed successfully.",
                "summary": result["summary"],
            },
            status=status.HTTP_200_OK,
        )


class ImportValidationIssueListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    List validation issues for one batch.
    Optional query param: ?severity=error
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportValidationIssueListSerializer

    def get_queryset(self):
        queryset = ImportValidationIssue.objects.filter(
            import_batch=self.get_import_batch()
        ).order_by("row_number", "created_at")

        severity = self.request.query_params.get("severity")
        if severity:
            queryset = queryset.filter(severity=severity)

        return queryset


class ImportValidationIssueDetailView(ImportBatchContextMixin, generics.RetrieveAPIView):
    """
    Get one validation issue.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportValidationIssueDetailSerializer
    lookup_url_kwarg = "issue_id"

    def get_queryset(self):
        return ImportValidationIssue.objects.filter(import_batch=self.get_import_batch())


class ResolveImportValidationIssueView(ImportBatchContextMixin, generics.UpdateAPIView):
    """
    Mark one issue as resolved.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportValidationIssueResolveSerializer
    lookup_url_kwarg = "issue_id"

    def get_queryset(self):
        return ImportValidationIssue.objects.filter(import_batch=self.get_import_batch())


# =========================================================
# Row Correction Views
# =========================================================
class ImportRowCorrectionListCreateView(ImportBatchContextMixin, generics.ListCreateAPIView):
    """
    GET  -> list row corrections
    POST -> add a row correction
    """
    permission_classes = [IsAuthenticatedStaff]

    def get_queryset(self):
        return ImportRowCorrection.objects.filter(
            import_batch=self.get_import_batch()
        ).select_related("corrected_by").order_by("row_number", "created_at")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ImportRowCorrectionCreateSerializer
        return ImportRowCorrectionSerializer


class RevalidateAfterCorrectionView(ImportBatchContextMixin, APIView):
    """
    Re-run validation after manual corrections.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()

        serializer = RevalidateAfterCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = validate_import_batch(import_batch)

        return Response(
            {
                "message": "Revalidation completed successfully.",
                "summary": result["summary"],
            },
            status=status.HTTP_200_OK,
        )


# =========================================================
# Import Job Views
# =========================================================
class StartImportBatchView(ImportBatchContextMixin, APIView):
    """
    Start actual import execution.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id):
        import_batch = self.get_import_batch()

        serializer = StartImportSerializer(
            data=request.data,
            context={"import_batch": import_batch},
        )
        serializer.is_valid(raise_exception=True)

        job = execute_import(import_batch=import_batch, queued_by=request.user)

        return Response(
            {
                "message": "Import started successfully.",
                "job_id": str(job.id),
                "job_status": job.status,
            },
            status=status.HTTP_200_OK,
        )


class ImportJobListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    List jobs for one import batch.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportJobListSerializer

    def get_queryset(self):
        return ImportJob.objects.filter(
            import_batch=self.get_import_batch()
        ).select_related("queued_by").order_by("-created_at")


class ImportJobDetailView(ImportJobContextMixin, generics.RetrieveAPIView):
    """
    Get one import job with row results.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportJobDetailSerializer

    def get_queryset(self):
        return ImportJob.objects.filter(
            import_batch=self.get_import_batch()
        ).select_related("queued_by").prefetch_related("row_results")


class RollbackImportJobView(ImportJobContextMixin, APIView):
    """
    Roll back a completed import job.
    """
    permission_classes = [IsAuthenticatedStaff]

    def post(self, request, institution_id, batch_id, job_id):
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

        return Response(
            {
                "message": "Rollback completed successfully.",
                "rollback_id": str(rollback_record.id),
                "reverted_rows_count": rollback_record.reverted_rows_count,
            },
            status=status.HTTP_200_OK,
        )


class ImportRollbackRecordListView(ImportJobContextMixin, generics.ListAPIView):
    """
    List rollback history for one job.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportRollbackRecordSerializer

    def get_queryset(self):
        return ImportRollbackRecord.objects.filter(job=self.get_job()).order_by("-started_at")


# =========================================================
# Audit / Notification Views
# =========================================================
class ImportAuditLogListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    List audit logs for one batch.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportAuditLogSerializer

    def get_queryset(self):
        return ImportAuditLog.objects.filter(
            import_batch=self.get_import_batch()
        ).select_related("actor").order_by("-created_at")


class ImportNotificationListView(ImportBatchContextMixin, generics.ListAPIView):
    """
    List notifications for one batch.
    """
    permission_classes = [IsAuthenticatedStaff]
    serializer_class = ImportNotificationSerializer

    def get_queryset(self):
        return ImportNotification.objects.filter(
            import_batch=self.get_import_batch()
        ).select_related("recipient").order_by("-created_at")