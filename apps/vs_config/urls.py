from django.urls import path

from . import views

urlpatterns = [
    # Global configuration keys
    path("keys/", views.ConfigurationKeyListCreateView.as_view(), name="config-key-list-create"),
    path("keys/<str:key>/restore/", views.ConfigurationKeyRestoreView.as_view(), name="config-key-restore"),
    path("keys/<str:key>/", views.ConfigurationKeyDetailView.as_view(), name="config-key-detail"),

    # Branch feature flags
    path("branches/<str:branch_id>/flags/history/", views.BranchFlagHistoryView.as_view(), name="branch-flag-history"),
    path("branches/<str:branch_id>/flags/<str:flag_key>/", views.BranchFlagToggleView.as_view(), name="branch-flag-toggle"),
    path("branches/<str:branch_id>/flags/", views.BranchFlagListView.as_view(), name="branch-flag-list"),

    # Branch self-service overrides
    path("my-branch/overrides/history/", views.BranchOverrideHistoryView.as_view(), name="branch-override-history"),
    path("my-branch/overrides/", views.BranchOverrideView.as_view(), name="branch-override"),

    # Export
    path("export/", views.ConfigExportView.as_view(), name="config-export"),
]
