from django.urls import path

from .views.package import PackagePlanListView, XVSModuleListView

from .views.school import (
    SchoolCreateView,
    SchoolDetailView,
    SchoolListView,
    SchoolUpdateView,
    SchoolStatsView,
)
from .views.branch import (
    BranchListView,
    BranchCreateView, 
    BranchDetailView,
    BranchStatsView, 
    BranchUpdateView
)
from .views.lifecycle import BranchTransitionView
from .views.ops import (
    SchoolResetConfigView,
)

urlpatterns = [
    # --------- Schools ---------
    path("", SchoolListView.as_view(), name="school-list"),
    path("create/", SchoolCreateView.as_view(), name="school-create"),
    path("stats/", SchoolStatsView.as_view(), name="school-stats"),

    # --------- Package Plans & Modules ---------
    path("package-plans/", PackagePlanListView.as_view(), name="package-plan-list"),
    path("modules/", XVSModuleListView.as_view(), name="xvs-module-list"),

    # --------- School record access ---------
    path("<str:slug>/", SchoolDetailView.as_view(), name="school-detail"),
    path("<str:slug>/update/", SchoolUpdateView.as_view(), name="school-update"),
    path("<str:slug>/reset-config/", SchoolResetConfigView.as_view(), name="school-reset-config"),

    # --------- Branches ---------
    path("<str:slug>/branches/", BranchListView.as_view(), name="branch-list"),
    path("<str:slug>/branches/create/", BranchCreateView.as_view(), name="branch-create"),
    path("<str:slug>/branches/stats/", BranchStatsView.as_view(), name="branch-stats"),
    path("<str:slug>/branches/<int:code>/detail/", BranchDetailView.as_view(), name="branch-detail"),
    path("<str:slug>/branches/<int:code>/update/", BranchUpdateView.as_view(), name="branch-update"),
    path("<str:slug>/branches/<int:code>/transition/", BranchTransitionView.as_view(), name="branch-transition"),
]
