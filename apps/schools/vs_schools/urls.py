from django.urls import path

from .views.package import PackagePlanListView, XVSModuleListView

from .views.school import (
    SchoolCreateView,
    SchoolDetailView,
    SchoolListView,
    SchoolLogoView,
    SchoolProfileView,
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
    SchoolServiceStateView,
)

urlpatterns = [
    # --------- Schools ---------
    path("", SchoolListView.as_view(), name="school-list"),
    path("create/", SchoolCreateView.as_view(), name="school-create"),
    path("stats/", SchoolStatsView.as_view(), name="school-stats"),

    # --------- Package Plans & Modules ---------
    path("package-plans/", PackagePlanListView.as_view(), name="package-plan-list"),
    path("modules/", XVSModuleListView.as_view(), name="xvs-module-list"),

    # --------- The caller's own school ---------
    # Two segments, so it can never be shadowed by the ``<str:slug>/`` route
    # below however a school is named. A single "profile/" would have been
    # taken by any school whose slug was "profile" - and "profile" is not on
    # the reserved list.
    path("me/profile/", SchoolProfileView.as_view(), name="school-profile"),
    path("me/profile/logo/", SchoolLogoView.as_view(), name="school-profile-logo"),

    # --------- School record access ---------
    path("<str:slug>/", SchoolDetailView.as_view(), name="school-detail"),
    path("<str:slug>/update/", SchoolUpdateView.as_view(), name="school-update"),
    path("<str:slug>/reset-config/", SchoolResetConfigView.as_view(), name="school-reset-config"),
    path("<str:slug>/service-state/", SchoolServiceStateView.as_view(), name="school-service-state"),

    # --------- Branches ---------
    path("<str:slug>/branches/", BranchListView.as_view(), name="branch-list"),
    path("<str:slug>/branches/create/", BranchCreateView.as_view(), name="branch-create"),
    path("<str:slug>/branches/stats/", BranchStatsView.as_view(), name="branch-stats"),
    path("<str:slug>/branches/<int:code>/detail/", BranchDetailView.as_view(), name="branch-detail"),
    path("<str:slug>/branches/<int:code>/update/", BranchUpdateView.as_view(), name="branch-update"),
    path("<str:slug>/branches/<int:code>/transition/", BranchTransitionView.as_view(), name="branch-transition"),
]
