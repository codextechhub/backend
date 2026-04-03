from django.urls import path

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
    path("branches/", BranchListView.as_view(), name="branch-list"),
    path("<str:slug>/branches/", BranchCreateView.as_view(), name="branch-create"),
    path("branches/stats/", BranchStatsView.as_view(), name="branch-stats"),

    # Branch record access (separate detail/update/delete views)
    path("<str:slug>/branches/<int:code>/detail/", BranchDetailView.as_view(), name="branch-detail"),
    path("<str:slug>/branches/<int:code>/update/", BranchUpdateView.as_view(), name="branch-update"),
    path("<str:slug>/branches/<int:code>/transition/", BranchTransitionView.as_view(), name="branch-transition"),
]
