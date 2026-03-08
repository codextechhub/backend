from django.urls import path

from .views.institution import (
    InstitutionCreateView,
    InstitutionDetailView,
    InstitutionListView,
    InstitutionUpdateView,
    InstitutionCountView,
)
from .views.branch import (
    BranchListView, 
    BranchCountView, 
    BranchCreateView, 
    BranchDetailView, 
    BranchUpdateView
)
from .views.lifecycle import BranchTransitionView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views.ops import (
    InstitutionResetConfigView,
)

urlpatterns = [
    # Authentication
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # --------- Institutions ---------
    # Institutions (separate list/create views)
    path("", InstitutionListView.as_view(), name="institution-list"),
    path("create/", InstitutionCreateView.as_view(), name="institution-create"),
    path("count/", InstitutionCountView.as_view(), name="institution-count-param"),

    # Institution record access (separate detail/update/delete views)
    path("<str:slug>/detail/", InstitutionDetailView.as_view(), name="institution-detail"),
    path("<str:slug>/update/", InstitutionUpdateView.as_view(), name="institution-update"),

    # Lifecycle / Reset
    path("<str:slug>/reset-config/", InstitutionResetConfigView.as_view(), name="institution-reset-config"),

    # --------- Branches ---------
    # Branches (separate list/create views)
    path("branches/", BranchListView.as_view(), name="branch-list"),
    path("<str:i_slug>/branches/create/", BranchCreateView.as_view(), name="branch-create"),
    path("branches/count/", BranchCountView.as_view(), name="branch-count-param"),

    # Branch record access (separate detail/update/delete views)
    path("<str:i_slug>/branches/<int:code>/detail/", BranchDetailView.as_view(), name="branch-detail"),
    path("<str:i_slug>/branches/<int:code>/update/", BranchUpdateView.as_view(), name="branch-update"),
    path("<str:i_slug>/branches/<int:code>/transition/", BranchTransitionView.as_view(), name="branch-transition"),
]
