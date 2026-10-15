from django.urls import path

from .views.amendments import (
    AmendmentApproveView,
    AmendmentFormView,
    AmendmentRejectView,
    AmendmentsView,
)
from .views.analytics import AnalyticsExportView, AnalyticsView
from .views.classes import (
    ApprovalsView,
    BroadsheetView,
    ClassReturnView,
    ClassSubmitView,
    CompileView,
    PrincipalCommentsView,
    PublishPreviewView,
    PublishView,
)
from .views.holds import HoldLiftView, HoldsView
from .views.hubs import AdminHubView, TeacherHubView
from .views.moderation import (
    ModerationQueueView,
    SheetApproveView,
    SheetReturnView,
)
from .views.results import (
    ClassCardsView,
    ReleaseClassesView,
    ReleaseListView,
    ReportCardView,
    StudentResultsView,
)
from .views.settings import SettingsView
from .views.sheets import (
    SheetImportConfirmView,
    SheetImportView,
    SheetRemindView,
    SheetScoresView,
    SheetSubmitView,
    SheetTemplateView,
    SheetView,
)
from .views.setup import (
    RemarkTemplatesView,
    ScaleActivateView,
    ScaleCopyView,
    ScaleDetailView,
    ScaleListCreateView,
    StructureActivateView,
    StructureCopyView,
    StructureDetailView,
    StructureListCreateView,
)

urlpatterns = [
    path("structures/", StructureListCreateView.as_view(), name="gradebook-structures"),
    path(
        "structures/<int:pk>/", StructureDetailView.as_view(), name="gradebook-structure",
    ),
    path(
        "structures/<int:pk>/copy/", StructureCopyView.as_view(),
        name="gradebook-structure-copy",
    ),
    path(
        "structures/<int:pk>/activate/", StructureActivateView.as_view(),
        name="gradebook-structure-activate",
    ),
    path("scales/", ScaleListCreateView.as_view(), name="gradebook-scales"),
    path("scales/<int:pk>/", ScaleDetailView.as_view(), name="gradebook-scale"),
    path(
        "scales/<int:pk>/copy/", ScaleCopyView.as_view(), name="gradebook-scale-copy",
    ),
    path(
        "scales/<int:pk>/activate/", ScaleActivateView.as_view(),
        name="gradebook-scale-activate",
    ),
    path("remark-templates/", RemarkTemplatesView.as_view(), name="gradebook-remark-templates"),
    path("settings/", SettingsView.as_view(), name="gradebook-settings"),
    path("hub/", AdminHubView.as_view(), name="gradebook-hub"),
    path("hub/mine/", TeacherHubView.as_view(), name="gradebook-hub-mine"),
    path("moderation/", ModerationQueueView.as_view(), name="gradebook-moderation"),
    path(
        "sheets/<int:class_id>/<int:subject_id>/", SheetView.as_view(),
        name="gradebook-sheet",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/scores/", SheetScoresView.as_view(),
        name="gradebook-sheet-scores",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/submit/", SheetSubmitView.as_view(),
        name="gradebook-sheet-submit",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/template/", SheetTemplateView.as_view(),
        name="gradebook-sheet-template",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/import/", SheetImportView.as_view(),
        name="gradebook-sheet-import",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/import/<slug:token>/confirm/",
        SheetImportConfirmView.as_view(), name="gradebook-sheet-import-confirm",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/approve/", SheetApproveView.as_view(),
        name="gradebook-sheet-approve",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/return/", SheetReturnView.as_view(),
        name="gradebook-sheet-return",
    ),
    path(
        "sheets/<int:class_id>/<int:subject_id>/remind/", SheetRemindView.as_view(),
        name="gradebook-sheet-remind",
    ),
    path(
        "classes/<int:class_id>/broadsheet/", BroadsheetView.as_view(),
        name="gradebook-broadsheet",
    ),
    path(
        "classes/<int:class_id>/students/<int:student_id>/compile/", CompileView.as_view(),
        name="gradebook-compile",
    ),
    path(
        "classes/<int:class_id>/submit/", ClassSubmitView.as_view(),
        name="gradebook-class-submit",
    ),
    path(
        "classes/<int:class_id>/return/", ClassReturnView.as_view(),
        name="gradebook-class-return",
    ),
    path(
        "classes/<int:class_id>/principal-comments/", PrincipalCommentsView.as_view(),
        name="gradebook-principal-comments",
    ),
    path(
        "classes/<int:class_id>/publish/preview/", PublishPreviewView.as_view(),
        name="gradebook-publish-preview",
    ),
    path("classes/publish/", PublishView.as_view(), name="gradebook-publish"),
    path("approvals/", ApprovalsView.as_view(), name="gradebook-approvals"),
    path("amendments/", AmendmentsView.as_view(), name="gradebook-amendments"),
    path("amendments/form/", AmendmentFormView.as_view(), name="gradebook-amendment-form"),
    path(
        "amendments/<int:pk>/approve/", AmendmentApproveView.as_view(),
        name="gradebook-amendment-approve",
    ),
    path(
        "amendments/<int:pk>/reject/", AmendmentRejectView.as_view(),
        name="gradebook-amendment-reject",
    ),
    path("analytics/", AnalyticsView.as_view(), name="gradebook-analytics"),
    path(
        "analytics/export/", AnalyticsExportView.as_view(), name="gradebook-analytics-export",
    ),
    path("holds/", HoldsView.as_view(), name="gradebook-holds"),
    path("holds/<int:hold_id>/lift/", HoldLiftView.as_view(), name="gradebook-hold-lift"),
    path("release/", ReleaseClassesView.as_view(), name="gradebook-release-classes"),
    path("release/<int:class_id>/", ReleaseListView.as_view(), name="gradebook-release"),
    path(
        "report-cards/<int:class_id>/", ClassCardsView.as_view(), name="gradebook-report-cards",
    ),
    path(
        "report-cards/<int:class_id>/<int:student_id>/", ReportCardView.as_view(),
        name="gradebook-report-card",
    ),
    path(
        "students/<int:student_id>/results/", StudentResultsView.as_view(),
        name="gradebook-student-results",
    ),
]
