from django.urls import path

from . import views


urlpatterns = [
    # =========================================================
    # Official System Import Templates
    # =========================================================
    path(
        "system-import-templates/",
        views.SystemImportTemplateListView.as_view(),
        name="system-import-template-list",
    ),
    path(
        "system-import-templates/<int:template_id>/",
        views.SystemImportTemplateDetailView.as_view(),
        name="system-import-template-detail",
    ),
    path(
        "system-import-templates/<int:template_id>/download/",
        views.SystemImportTemplateDownloadView.as_view(),
        name="system-import-template-download",
    ),

    # =========================================================
    # Import Batches
    # =========================================================
    path(
        "batches/",
        views.ImportBatchListCreateView.as_view(),
        name="import-batch-list-create",
    ),
    path(
        "batches/<int:batch_id>/",
        views.ImportBatchDetailView.as_view(),
        name="import-batch-detail",
    ),

    # =========================================================
    # Validation
    # =========================================================
    path(
        "batches/<int:batch_id>/validate/",
        views.ValidateImportBatchView.as_view(),
        name="import-batch-validate",
    ),
    path(
        "batches/<int:batch_id>/issues/",
        views.ImportValidationIssueListView.as_view(),
        name="import-validation-issue-list",
    ),
    path(
        "batches/<int:batch_id>/issues/<int:issue_id>/",
        views.ImportValidationIssueDetailView.as_view(),
        name="import-validation-issue-detail",
    ),
    path(
        "batches/<int:batch_id>/issues/<int:issue_id>/resolve/",
        views.ResolveImportValidationIssueView.as_view(),
        name="import-validation-issue-resolve",
    ),

    # =========================================================
    # Row Corrections
    # =========================================================
    path(
        "batches/<int:batch_id>/corrections/",
        views.ImportRowCorrectionListCreateView.as_view(),
        name="import-row-correction-list-create",
    ),
    path(
        "batches/<int:batch_id>/revalidate/",
        views.RevalidateAfterCorrectionView.as_view(),
        name="import-batch-revalidate",
    ),

    # =========================================================
    # Import Jobs
    # =========================================================
    path(
        "batches/<int:batch_id>/start-import/",
        views.StartImportBatchView.as_view(),
        name="import-batch-start",
    ),
    path(
        "batches/<int:batch_id>/jobs/",
        views.ImportJobListView.as_view(),
        name="import-job-list",
    ),
    path(
        "batches/<int:batch_id>/jobs/<int:job_id>/",
        views.ImportJobDetailView.as_view(),
        name="import-job-detail",
    ),
    path(
        "batches/<int:batch_id>/jobs/<int:job_id>/rollback/",
        views.RollbackImportJobView.as_view(),
        name="import-job-rollback",
    ),
    path(
        "batches/<int:batch_id>/jobs/<int:job_id>/rollbacks/",
        views.ImportRollbackRecordListView.as_view(),
        name="import-rollback-record-list",
    ),

    # =========================================================
    # Audit / Notifications
    # =========================================================
    path(
        "batches/<int:batch_id>/audit-logs/",
        views.ImportAuditLogListView.as_view(),
        name="import-audit-log-list",
    ),
    path(
        "batches/<int:batch_id>/notifications/",
        views.ImportNotificationListView.as_view(),
        name="import-notification-list",
    ),
]
