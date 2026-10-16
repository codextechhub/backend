"""Routes for the pupil portal, mounted at ``/v1/portal/student/``.

``notices/read-all/`` is declared before ``notices/<int:pk>/`` so it resolves
as itself.
"""
from django.urls import path

from .views import (
    AttendanceView,
    MeView,
    NoticeDetailView,
    NoticeFeedView,
    NoticeReadAllView,
    NoticeReadView,
    ProfilePhotoView,
    ProfileView,
    ReportCardView,
    ResultsView,
    SessionResultsView,
    TimetableView,
)

urlpatterns = [
    path("me/", MeView.as_view(), name="student-portal-me"),
    path("profile/", ProfileView.as_view(), name="student-portal-profile"),
    path(
        "profile/photo/", ProfilePhotoView.as_view(),
        name="student-portal-profile-photo",
    ),
    path("timetable/", TimetableView.as_view(), name="student-portal-timetable"),
    path("attendance/", AttendanceView.as_view(), name="student-portal-attendance"),
    path("results/", ResultsView.as_view(), name="student-portal-results"),
    path(
        "results/session/", SessionResultsView.as_view(),
        name="student-portal-results-session",
    ),
    path(
        "results/report-card/", ReportCardView.as_view(),
        name="student-portal-report-card",
    ),
    path("notices/", NoticeFeedView.as_view(), name="student-portal-notices"),
    path(
        "notices/read-all/", NoticeReadAllView.as_view(),
        name="student-portal-notices-read-all",
    ),
    path(
        "notices/<int:pk>/", NoticeDetailView.as_view(),
        name="student-portal-notice",
    ),
    path(
        "notices/<int:pk>/read/", NoticeReadView.as_view(),
        name="student-portal-notice-read",
    ),
]
