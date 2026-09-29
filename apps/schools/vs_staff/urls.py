"""Routes for the staff surface, mounted at ``/v1/i/me/staff/``.

**Every literal segment is declared before ``<int:pk>``.** Django tries patterns
in list order, so ``posting/``, ``roster/``, ``search/``, ``roles/bulk/``,
``rules/``, ``number-policy/``, ``teaching/coverage/``, ``organogram/`` and the
four child-detail prefixes would each be swallowed by the pk pattern and answer a confusing 404 about a
person who does not exist. The same trap inside ``teaching/`` is why
``coverage/`` and ``class-teacher/`` precede ``<int:pk>/`` there too, and inside
``organogram/`` why ``tree/``, ``vacancies/``, ``current/`` and ``mine/`` do.
``tests/test_urls.py`` asserts every one of them.
"""
from django.urls import path

from .views.directory import (
    StaffDetailView,
    StaffListCreateView,
    StaffMineView,
    StaffSearchView,
)
from .views.leave import LeaveDetailView, StaffLeaveView
from .views.organogram import (
    AppointmentCloseView,
    AppointmentDetailView,
    AppointmentListCreateView,
    CurrentAppointmentsView,
    MatrixReportDetailView,
    MatrixReportListCreateView,
    MyAppointmentsView,
    OrganogramSummaryView,
    OrgNodeDetailView,
    OrgNodeListCreateView,
    PositionDetailView,
    PositionListCreateView,
    PositionTreeView,
    PositionVacanciesView,
)
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
from .views.settings import StaffNumberPolicyView, StaffRulesView
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
    path("mine/", StaffMineView.as_view(), name="staff-mine"),
    path("roles/bulk/", StaffBulkRoleView.as_view(), name="staff-bulk-role"),
    path("rules/", StaffRulesView.as_view(), name="staff-rules"),
    path(
        "number-policy/", StaffNumberPolicyView.as_view(),
        name="staff-number-policy",
    ),

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

    # ── The organogram, under a literal prefix of its own ─────────────────
    path(
        "organogram/nodes/", OrgNodeListCreateView.as_view(),
        name="staff-org-nodes",
    ),
    path(
        "organogram/nodes/<int:pk>/", OrgNodeDetailView.as_view(),
        name="staff-org-node-detail",
    ),
    path(
        "organogram/positions/", PositionListCreateView.as_view(),
        name="staff-org-positions",
    ),
    path(
        "organogram/positions/tree/", PositionTreeView.as_view(),
        name="staff-org-tree",
    ),
    path(
        "organogram/positions/vacancies/", PositionVacanciesView.as_view(),
        name="staff-org-vacancies",
    ),
    path(
        "organogram/positions/<int:pk>/", PositionDetailView.as_view(),
        name="staff-org-position-detail",
    ),
    path(
        "organogram/assignments/", AppointmentListCreateView.as_view(),
        name="staff-org-assignments",
    ),
    path(
        "organogram/assignments/current/", CurrentAppointmentsView.as_view(),
        name="staff-org-assignments-current",
    ),
    path(
        "organogram/assignments/mine/", MyAppointmentsView.as_view(),
        name="staff-org-assignments-mine",
    ),
    path(
        "organogram/assignments/<int:pk>/", AppointmentDetailView.as_view(),
        name="staff-org-assignment-detail",
    ),
    path(
        "organogram/assignments/<int:pk>/close/", AppointmentCloseView.as_view(),
        name="staff-org-assignment-close",
    ),
    path(
        "organogram/matrix-reports/", MatrixReportListCreateView.as_view(),
        name="staff-org-matrix-reports",
    ),
    path(
        "organogram/matrix-reports/<int:pk>/", MatrixReportDetailView.as_view(),
        name="staff-org-matrix-report-detail",
    ),
    path(
        "organogram/summary/", OrganogramSummaryView.as_view(),
        name="staff-org-summary",
    ),

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
