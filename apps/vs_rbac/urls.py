from django.urls import path

from . import views

urlpatterns = [
    # -------------------------
    # Vision-owned Permission Registry
    # -------------------------
    path("vision/permissions/", views.PermissionListCreateView.as_view(), name="rbac-permission-list-create"),
    path("vision/permissions/<str:key>/", views.PermissionDetailView.as_view(), name="rbac-permission-detail"),
    path(
        "vision/permission-dependencies/",
        views.PermissionDependencyListCreateView.as_view(),
        name="rbac-permission-dependency-list-create",
    ),
    path(
        "vision/permission-dependencies/<int:pk>/",
        views.PermissionDependencyDetailView.as_view(),
        name="rbac-permission-dependency-detail",
    ),

    # -------------------------
    # Institution-scoped Roles
    # -------------------------
    path(
        "institutions/<int:institution_id>/roles/",
        views.RoleTemplateListCreateView.as_view(),
        name="rbac-role-list-create",
    ),
    path(
        "institutions/<int:institution_id>/roles/<int:id>/",
        views.RoleTemplateDetailView.as_view(),
        name="rbac-role-detail",
    ),

    # Role snapshots (rollback)
    path(
        "institutions/<int:institution_id>/roles/<int:role_id>/snapshots/",
        views.RoleSnapshotListView.as_view(),
        name="rbac-role-snapshot-list",
    ),
    path(
        "institutions/<int:institution_id>/role-snapshots/<int:id>/",
        views.RoleSnapshotDetailView.as_view(),
        name="rbac-role-snapshot-detail",
    ),

    # Role assignments
    path(
        "institutions/<int:institution_id>/role-assignments/",
        views.UserRoleAssignmentListCreateView.as_view(),
        name="rbac-assignment-list-create",
    ),
    path(
        "institutions/<int:institution_id>/role-assignments/<int:id>/",
        views.UserRoleAssignmentDetailView.as_view(),
        name="rbac-assignment-detail",
    ),

    # Institution role-change requests
    path(
        "institutions/<int:institution_id>/role-change-requests/",
        views.InstitutionRoleChangeRequestListCreateView.as_view(),
        name="rbac-role-change-request-list-create",
    ),

    # Vision review queue + decision endpoint
    path(
        "vision/role-change-requests/",
        views.VisionRoleChangeRequestQueueView.as_view(),
        name="rbac-vision-role-change-queue",
    ),
    path(
        "vision/role-change-requests/<int:request_id>/decide/",
        views.VisionRoleChangeRequestDecisionView.as_view(),
        name="rbac-vision-role-change-decide",
    ),

    # Lock history
    path(
        "institutions/<int:institution_id>/role-lock-events/",
        views.RoleLockEventListView.as_view(),
        name="rbac-role-lock-events",
    ),

    # Effective permission cache (optional)
    path(
        "institutions/<int:institution_id>/permission-cache/<int:id>/",
        views.EffectivePermissionCacheDetailView.as_view(),
        name="rbac-permission-cache-detail",
    ),
]