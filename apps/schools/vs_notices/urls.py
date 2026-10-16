"""Routes for notices, mounted at ``/v1/notices/``.

``audiences/`` is declared before ``<int:pk>/`` so it resolves as itself.
"""
from django.urls import path

from .views import (
    NoticeAudienceOptionsView,
    NoticeDetailView,
    NoticeListCreateView,
    NoticePublishView,
    NoticeWithdrawView,
)

urlpatterns = [
    path("", NoticeListCreateView.as_view(), name="notice-list"),
    path("audiences/", NoticeAudienceOptionsView.as_view(), name="notice-audiences"),
    path("<int:pk>/", NoticeDetailView.as_view(), name="notice-detail"),
    path("<int:pk>/publish/", NoticePublishView.as_view(), name="notice-publish"),
    path("<int:pk>/withdraw/", NoticeWithdrawView.as_view(), name="notice-withdraw"),
]
