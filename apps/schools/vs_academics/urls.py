from django.urls import path

from .views import (
    ClassArchiveView,
    ClassDetailView,
    ClassListCreateView,
    ClassRestoreView,
    DepartmentDetailView,
    DepartmentListCreateView,
    LevelBulkCreateView,
    LevelDetailView,
    GenerateArmsView,
    LevelListCreateView,
    OverviewView,
    ProgramDetailView,
    ProgramListCreateView,
    SessionActivateView,
    SessionArchiveView,
    SessionDetailView,
    SessionListCreateView,
    StructureTreeView,
    SubjectDetailView,
    SubjectListCreateView,
    SubjectOfferingsView,
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

    path("departments/", DepartmentListCreateView.as_view(), name="academics-department-list"),
    path("departments/<int:pk>/", DepartmentDetailView.as_view(), name="academics-department-detail"),

    path("programs/", ProgramListCreateView.as_view(), name="academics-program-list"),
    path("programs/<int:pk>/", ProgramDetailView.as_view(), name="academics-program-detail"),
    path("programs/<int:pk>/levels/", LevelListCreateView.as_view(), name="academics-level-list"),
    path("programs/<int:pk>/levels/bulk/", LevelBulkCreateView.as_view(), name="academics-level-bulk"),
    path("levels/<int:pk>/", LevelDetailView.as_view(), name="academics-level-detail"),

    path("classes/", ClassListCreateView.as_view(), name="academics-class-list"),
    path("classes/generate-arms/", GenerateArmsView.as_view(), name="academics-class-arms"),
    path("classes/<int:pk>/", ClassDetailView.as_view(), name="academics-class-detail"),
    path("classes/<int:pk>/archive/", ClassArchiveView.as_view(), name="academics-class-archive"),
    path("classes/<int:pk>/restore/", ClassRestoreView.as_view(), name="academics-class-restore"),

    path("subjects/", SubjectListCreateView.as_view(), name="academics-subject-list"),
    path("subjects/<int:pk>/", SubjectDetailView.as_view(), name="academics-subject-detail"),
    path("subjects/<int:pk>/offerings/", SubjectOfferingsView.as_view(), name="academics-subject-offerings"),

    path("structure/tree/", StructureTreeView.as_view(), name="academics-structure-tree"),
    path("overview/", OverviewView.as_view(), name="academics-overview"),
]
