"""Routes for the parent portal, mounted at ``/v1/portal/parent/``.

``notices/read-all/`` is declared before ``notices/<int:pk>/`` so it resolves
as itself.
"""
from django.urls import path

from .views import (
    AttendanceView,
    ChildRecordView,
    MeView,
    NoticeDetailView,
    NoticeFeedView,
    NoticeReadAllView,
    NoticeReadView,
    ReportCardView,
    ResultsView,
    SessionResultsView,
    TimetableView,
)

urlpatterns = [
    path("me/", MeView.as_view(), name="parent-portal-me"),
    path("children/<int:child_id>/", ChildRecordView.as_view(), name="parent-portal-child"),
    path(
        "children/<int:child_id>/timetable/", TimetableView.as_view(),
        name="parent-portal-timetable",
    ),
    path(
        "children/<int:child_id>/attendance/", AttendanceView.as_view(),
        name="parent-portal-attendance",
    ),
    path(
        "children/<int:child_id>/results/", ResultsView.as_view(),
        name="parent-portal-results",
    ),
    path(
        "children/<int:child_id>/results/session/", SessionResultsView.as_view(),
        name="parent-portal-results-session",
    ),
    path(
        "children/<int:child_id>/results/report-card/", ReportCardView.as_view(),
        name="parent-portal-report-card",
    ),
    path("notices/", NoticeFeedView.as_view(), name="parent-portal-notices"),
    path(
        "notices/read-all/", NoticeReadAllView.as_view(),
        name="parent-portal-notices-read-all",
    ),
    path("notices/<int:pk>/", NoticeDetailView.as_view(), name="parent-portal-notice"),
    path(
        "notices/<int:pk>/read/", NoticeReadView.as_view(),
        name="parent-portal-notice-read",
    ),
]
