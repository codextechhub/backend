from django.urls import path

from .views import (
    SessionActivateView,
    SessionArchiveView,
    SessionDetailView,
    SessionListCreateView,
    TermDetailView,
    TermListCreateView,
)

urlpatterns = [
    path("sessions/", SessionListCreateView.as_view(), name="academics-session-list"),
    path("sessions/<int:pk>/", SessionDetailView.as_view(), name="academics-session-detail"),
    path("sessions/<int:pk>/activate/", SessionActivateView.as_view(), name="academics-session-activate"),
    path("sessions/<int:pk>/archive/", SessionArchiveView.as_view(), name="academics-session-archive"),
    path("sessions/<int:pk>/terms/", TermListCreateView.as_view(), name="academics-term-list"),
    # No archive route for a term, deliberately. See TermDetailView's docstring;
    # test_no_standalone_term_archive_route asserts the absence.
    path("terms/<int:pk>/", TermDetailView.as_view(), name="academics-term-detail"),
]
