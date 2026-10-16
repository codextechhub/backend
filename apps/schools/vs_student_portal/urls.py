"""Routes for the pupil portal, mounted at ``/v1/portal/student/``.

``notices/read-all/`` is declared before ``notices/<int:pk>/`` so it resolves
as itself.
"""
from django.urls import path

from .views import (
    MeView,
    NoticeDetailView,
    NoticeFeedView,
    NoticeReadAllView,
    NoticeReadView,
)

urlpatterns = [
    path("me/", MeView.as_view(), name="student-portal-me"),
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
