"""Routes for M11 Student Management.

Guardian routes live under ``/v1/guardians/`` because a guardian is reachable
from more than one student, so the tenant check on them cannot be inherited
from a student in the URL - it is made explicitly against ``request.tenant``.

The collection routes are declared **before** ``<int:pk>/`` so ``summary``,
``search``, ``unplaced``, ``promotions`` and ``bulk`` resolve as themselves
rather than being matched as a student id.
"""
from django.urls import path

from .views import (
    AdmissionPolicyView,
    AssignClassView,
    BulkAssignClassView,
    BulkStatusView,
    ChangeStatusView,
    ClassHistoryView,
    ClassRosterView,
    ConfirmApplicantView,
    GuardianDetailView,
    GuardianDirectoryView,
    GuardianStudentsView,
    PromotionBatchView,
    PromotionPreviewView,
    PromotionRunView,
    ReactivateStudentView,
    RejectApplicantView,
    StatusHistoryView,
    StudentDetailView,
    StudentDocumentDetailView,
    StudentDocumentsView,
    StudentGuardianDetailView,
    StudentGuardiansView,
    StudentHistoryView,
    StudentListCreateView,
    StudentSearchView,
    StudentSubjectsView,
    StudentSummaryView,
    SuspendStudentView,
    TransferOutView,
    UnplacedStudentsView,
    WithdrawStudentView,
)

student_patterns = [
    path("", StudentListCreateView.as_view(), name="student-list"),
    path("summary/", StudentSummaryView.as_view(), name="student-summary"),
    path("search/", StudentSearchView.as_view(), name="student-search"),
    path("unplaced/", UnplacedStudentsView.as_view(), name="student-unplaced"),
    path(
        "admission-number-policy/", AdmissionPolicyView.as_view(),
        name="student-admission-policy",
    ),
    path(
        "classes/<int:class_id>/roster/", ClassRosterView.as_view(),
        name="student-class-roster",
    ),

    path(
        "promotions/preview/", PromotionPreviewView.as_view(),
        name="student-promotion-preview",
    ),
    path("promotions/", PromotionRunView.as_view(), name="student-promotion-run"),
    path(
        "promotions/<int:pk>/", PromotionBatchView.as_view(),
        name="student-promotion-batch",
    ),

    path(
        "bulk/assign-class/", BulkAssignClassView.as_view(),
        name="student-bulk-assign",
    ),
    path("bulk/status/", BulkStatusView.as_view(), name="student-bulk-status"),

    path("<int:pk>/", StudentDetailView.as_view(), name="student-detail"),
    path("<int:pk>/confirm/", ConfirmApplicantView.as_view(), name="student-confirm"),
    path("<int:pk>/reject/", RejectApplicantView.as_view(), name="student-reject"),
    path("<int:pk>/withdraw/", WithdrawStudentView.as_view(), name="student-withdraw"),
    path("<int:pk>/suspend/", SuspendStudentView.as_view(), name="student-suspend"),
    path(
        "<int:pk>/reactivate/", ReactivateStudentView.as_view(),
        name="student-reactivate",
    ),
    path(
        "<int:pk>/transfer-out/", TransferOutView.as_view(),
        name="student-transfer-out",
    ),
    path("<int:pk>/status/", ChangeStatusView.as_view(), name="student-status"),
    path(
        "<int:pk>/assign-class/", AssignClassView.as_view(),
        name="student-assign-class",
    ),
    path(
        "<int:pk>/status-history/", StatusHistoryView.as_view(),
        name="student-status-history",
    ),
    path(
        "<int:pk>/class-history/", ClassHistoryView.as_view(),
        name="student-class-history",
    ),
    path("<int:pk>/history/", StudentHistoryView.as_view(), name="student-history"),
    path("<int:pk>/subjects/", StudentSubjectsView.as_view(), name="student-subjects"),
    path(
        "<int:pk>/guardians/", StudentGuardiansView.as_view(),
        name="student-guardians",
    ),
    path(
        "<int:pk>/guardians/<int:guardian_id>/",
        StudentGuardianDetailView.as_view(), name="student-guardian-detail",
    ),
    path(
        "<int:pk>/documents/", StudentDocumentsView.as_view(),
        name="student-documents",
    ),
    path(
        "<int:pk>/documents/<int:doc_id>/", StudentDocumentDetailView.as_view(),
        name="student-document-detail",
    ),
]

guardian_patterns = [
    path("", GuardianDirectoryView.as_view(), name="guardian-list"),
    path("<int:pk>/", GuardianDetailView.as_view(), name="guardian-detail"),
    path(
        "<int:pk>/students/", GuardianStudentsView.as_view(),
        name="guardian-students",
    ),
]

urlpatterns = student_patterns
