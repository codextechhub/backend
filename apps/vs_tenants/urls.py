from django.urls import path

from .views import BranchOptionListView

urlpatterns = [
    path("branches/", BranchOptionListView.as_view(), name="tenant-branch-options"),
]
