from django.urls import path

from .views.institutions import (
    InstitutionCreateView,
    InstitutionDetailView,
    InstitutionHardDeleteView,
    InstitutionListView,
    InstitutionUpdateView,
)
from .views.lifecycle import InstitutionTransitionView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views.ops import (
    InstitutionReactivateView,
    InstitutionResetConfigView,
    InstitutionSoftDeleteView,
    InstitutionSuspendView,
)

urlpatterns = [
    # Authentication
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # Institutions (separate list/create views)
    path("institutions/", InstitutionListView.as_view(), name="institution-list"),
    path("institutions/create/", InstitutionCreateView.as_view(), name="institution-create"),

    # Institution record access (separate detail/update/delete views)
    path("institutions/<uuid:id>/", InstitutionDetailView.as_view(), name="institution-detail"),
    path("institutions/<uuid:id>/update/", InstitutionUpdateView.as_view(), name="institution-update"),
    path("institutions/<uuid:id>/hard-delete/", InstitutionHardDeleteView.as_view(), name="institution-hard-delete"),

    # Lifecycle
    path("institutions/<uuid:id>/transition/", InstitutionTransitionView.as_view(), name="institution-transition"),

    # Operations / danger zone
    path("institutions/<uuid:id>/suspend/", InstitutionSuspendView.as_view(), name="institution-suspend"),
    path("institutions/<uuid:id>/reactivate/", InstitutionReactivateView.as_view(), name="institution-reactivate"),
    path("institutions/<uuid:id>/soft-delete/", InstitutionSoftDeleteView.as_view(), name="institution-soft-delete"),
    path("institutions/<uuid:id>/reset-config/", InstitutionResetConfigView.as_view(), name="institution-reset-config"),
]
