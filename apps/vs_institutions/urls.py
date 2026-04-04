from django.urls import path

from .views.package import PackagePlanListView, XVSModuleListView

from .views.institution import (
    InstitutionCreateView,
    InstitutionDetailView,
    InstitutionListView,
    InstitutionUpdateView,
    InstitutionStatsView,
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
    InstitutionResetConfigView,
)

urlpatterns = [
    # --------- Institutions ---------
    # Institutions (separate list/create views)
    path("", InstitutionListView.as_view(), name="institution-list"),
    path("create/", InstitutionCreateView.as_view(), name="institution-create"),
    path("stats/", InstitutionStatsView.as_view(), name="institution-stats"),

    # Institution record access (separate detail/update/delete views)
    path("<str:slug>/", InstitutionDetailView.as_view(), name="institution-detail"),
    path("<str:slug>/update/", InstitutionUpdateView.as_view(), name="institution-update"),

    # Lifecycle / Reset
    path("<str:slug>/reset-config/", InstitutionResetConfigView.as_view(), name="institution-reset-config"),

    # --------- Branches ---------
    # Branches (separate list/create views)
    path("<str:slug>/branches/", BranchListView.as_view(), name="branch-list"),
    path("<str:slug>/branches/create/", BranchCreateView.as_view(), name="branch-create"),
    path("<str:slug>/branches/stats/", BranchStatsView.as_view(), name="branch-stats"),

    # Branch record access (separate detail/update/delete views)
    path("<str:slug>/branches/<int:code>/detail/", BranchDetailView.as_view(), name="branch-detail"),
    path("<str:slug>/branches/<int:code>/update/", BranchUpdateView.as_view(), name="branch-update"),
    path("<str:slug>/branches/<int:code>/transition/", BranchTransitionView.as_view(), name="branch-transition"),

    # --------- Package Plans & Modules ---------
    path("package-plans/", PackagePlanListView.as_view(), name="package-plan-list"),
    path("modules/", XVSModuleListView.as_view(), name="xvs-module-list"),
]
