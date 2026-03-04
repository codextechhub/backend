from django.urls import path

from .views.institutions import (
    InstitutionCreateView,
    InstitutionDetailView,
    InstitutionListView,
    InstitutionUpdateView,
    InstitutionCountView,
)
from .views.lifecycle import InstitutionTransitionView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views.ops import (
    InstitutionResetConfigView,
)

urlpatterns = [
    # Authentication
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # Institutions (separate list/create views)
    path("institutions/", InstitutionListView.as_view(), name="institution-list"),
    path("institutions/create/", InstitutionCreateView.as_view(), name="institution-create"),
    path("institutions/count/", InstitutionCountView.as_view(), name="institution-count-param"),

    # Institution record access (separate detail/update/delete views)
    path("institutions/<str:slug>/", InstitutionDetailView.as_view(), name="institution-detail"),
    path("institutions/<str:slug>/update/", InstitutionUpdateView.as_view(), name="institution-update"),

    # Lifecycle / Reset
    path("institutions/<str:slug>/transition/", InstitutionTransitionView.as_view(), name="institution-transition"),
    path("institutions/<str:slug>/reset-config/", InstitutionResetConfigView.as_view(), name="institution-reset-config"),
]
