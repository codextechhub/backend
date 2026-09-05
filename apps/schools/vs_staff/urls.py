"""Routes for the staff surface, mounted at ``/v1/i/me/staff/``.

**Every literal segment is declared before ``<int:pk>``.** Django tries patterns
in list order, so ``posting/``, ``roster/``, ``search/``, ``roles/bulk/``,
``teaching/coverage/`` and the four child-detail prefixes would each be
swallowed by the pk pattern and answer a confusing 404 about a person who does
not exist. The same trap inside ``teaching/`` is why ``coverage/`` and
``class-teacher/`` precede ``<int:pk>/`` there too. ``tests/test_urls.py``
asserts every one of them.
"""
from django.urls import path

from .views.directory import StaffDetailView, StaffListCreateView, StaffSearchView
from .views.leave import LeaveDetailView, StaffLeaveView
from .views.lifecycle import (
    StaffAccountEmailView,
    StaffAccountReactivateView,
    StaffAccountSuspendView,
    StaffAccountUnlockView,
    StaffHistoryView,
    StaffInvitationRevokeView,
    StaffResendInvitationView,
    StaffStatusView,
)
from .views.posting import StaffBulkPostingView, StaffBulkRoleView, StaffRosterView
from .views.records import (
    DocumentDetailView,
    DocumentListCreateView,
    QualificationDetailView,
    QualificationListCreateView,
)
from .views.roles import StaffRolesView
from .views.teaching import (
    ClassTeacherView,
    StaffTeachingView,
    TeachingAssignmentDetailView,
    TeachingCoverageView,
)

urlpatterns = [
    # ── Literal segments, all before <int:pk> ─────────────────────────────
    path("posting/", StaffBulkPostingView.as_view(), name="staff-bulk-posting"),
    path("roster/", StaffRosterView.as_view(), name="staff-roster"),
    path("search/", StaffSearchView.as_view(), name="staff-search"),
    path("roles/bulk/", StaffBulkRoleView.as_view(), name="staff-bulk-role"),

    path(
        "teaching/coverage/", TeachingCoverageView.as_view(),
        name="staff-teaching-coverage",
    ),
    path(
        "teaching/class-teacher/", ClassTeacherView.as_view(),
        name="staff-class-teacher",
    ),
    path(
        "teaching/<int:pk>/", TeachingAssignmentDetailView.as_view(),
        name="staff-teaching-detail",
    ),

    path(
        "qualifications/<int:pk>/", QualificationDetailView.as_view(),
        name="staff-qualification-detail",
    ),
    path(
        "documents/<int:pk>/", DocumentDetailView.as_view(),
        name="staff-document-detail",
    ),
    path("leave/<int:pk>/", LeaveDetailView.as_view(), name="staff-leave-detail"),

    # ── The collection, and one person ────────────────────────────────────
    path("", StaffListCreateView.as_view(), name="staff-list"),
    path("<int:pk>/", StaffDetailView.as_view(), name="staff-detail"),
    path("<int:pk>/status/", StaffStatusView.as_view(), name="staff-status"),
    path("<int:pk>/history/", StaffHistoryView.as_view(), name="staff-history"),
    path("<int:pk>/roles/", StaffRolesView.as_view(), name="staff-roles"),
    path(
        "<int:pk>/qualifications/", QualificationListCreateView.as_view(),
        name="staff-qualifications",
    ),
    path(
        "<int:pk>/documents/", DocumentListCreateView.as_view(),
        name="staff-documents",
    ),
    path("<int:pk>/leave/", StaffLeaveView.as_view(), name="staff-leave"),
    path("<int:pk>/teaching/", StaffTeachingView.as_view(), name="staff-teaching"),

    path(
        "<int:pk>/account/suspend/", StaffAccountSuspendView.as_view(),
        name="staff-account-suspend",
    ),
    path(
        "<int:pk>/account/reactivate/", StaffAccountReactivateView.as_view(),
        name="staff-account-reactivate",
    ),
    path(
        "<int:pk>/account/unlock/", StaffAccountUnlockView.as_view(),
        name="staff-account-unlock",
    ),
    path(
        "<int:pk>/account/email/", StaffAccountEmailView.as_view(),
        name="staff-account-email",
    ),
    path(
        "<int:pk>/resend/", StaffResendInvitationView.as_view(),
        name="staff-resend",
    ),
    path(
        "<int:pk>/invitation/revoke/", StaffInvitationRevokeView.as_view(),
        name="staff-invitation-revoke",
    ),
]
