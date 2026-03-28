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
        "system-import-templates/<uuid:template_id>/",
        views.SystemImportTemplateDetailView.as_view(),
        name="system-import-template-detail",
    ),
    path(
        "system-import-templates/<uuid:template_id>/download/",
        views.SystemImportTemplateDownloadView.as_view(),
        name="system-import-template-download",
    ),

    # =========================================================
    # Import Batches
    # =========================================================
    path(
        "institutions/<int:institution_id>/imports/batches/",
        views.ImportBatchListCreateView.as_view(),
        name="import-batch-list-create",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/",
        views.ImportBatchDetailView.as_view(),
        name="import-batch-detail",
    ),

    # =========================================================
    # Validation
    # =========================================================
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/validate/",
        views.ValidateImportBatchView.as_view(),
        name="import-batch-validate",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/issues/",
        views.ImportValidationIssueListView.as_view(),
        name="import-validation-issue-list",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/issues/<uuid:issue_id>/",
        views.ImportValidationIssueDetailView.as_view(),
        name="import-validation-issue-detail",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/issues/<uuid:issue_id>/resolve/",
        views.ResolveImportValidationIssueView.as_view(),
        name="import-validation-issue-resolve",
    ),

    # =========================================================
    # Row Corrections
    # =========================================================
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/corrections/",
        views.ImportRowCorrectionListCreateView.as_view(),
        name="import-row-correction-list-create",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/revalidate/",
        views.RevalidateAfterCorrectionView.as_view(),
        name="import-batch-revalidate",
    ),

    # =========================================================
    # Import Jobs
    # =========================================================
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/start-import/",
        views.StartImportBatchView.as_view(),
        name="import-batch-start",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/jobs/",
        views.ImportJobListView.as_view(),
        name="import-job-list",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/jobs/<uuid:job_id>/",
        views.ImportJobDetailView.as_view(),
        name="import-job-detail",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/jobs/<uuid:job_id>/rollback/",
        views.RollbackImportJobView.as_view(),
        name="import-job-rollback",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/jobs/<uuid:job_id>/rollbacks/",
        views.ImportRollbackRecordListView.as_view(),
        name="import-rollback-record-list",
    ),

    # =========================================================
    # Audit / Notifications
    # =========================================================
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/audit-logs/",
        views.ImportAuditLogListView.as_view(),
        name="import-audit-log-list",
    ),
    path(
        "institutions/<int:institution_id>/imports/batches/<uuid:batch_id>/notifications/",
        views.ImportNotificationListView.as_view(),
        name="import-notification-list",
    ),
]