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
        "vision/permission-dependencies/<uuid:id>/",
        views.PermissionDependencyDetailView.as_view(),
        name="rbac-permission-dependency-detail",
    ),

    # -------------------------
    # Institution-scoped Roles
    # -------------------------
    path(
        "institutions/<uuid:institution_id>/roles/",
        views.RoleTemplateListCreateView.as_view(),
        name="rbac-role-list-create",
    ),
    path(
        "institutions/<uuid:institution_id>/roles/<uuid:id>/",
        views.RoleTemplateDetailView.as_view(),
        name="rbac-role-detail",
    ),

    # Role snapshots (rollback)
    path(
        "institutions/<uuid:institution_id>/roles/<uuid:role_id>/snapshots/",
        views.RoleSnapshotListView.as_view(),
        name="rbac-role-snapshot-list",
    ),
    path(
        "institutions/<uuid:institution_id>/role-snapshots/<uuid:id>/",
        views.RoleSnapshotDetailView.as_view(),
        name="rbac-role-snapshot-detail",
    ),

    # Role assignments
    path(
        "institutions/<uuid:institution_id>/role-assignments/",
        views.UserRoleAssignmentListCreateView.as_view(),
        name="rbac-assignment-list-create",
    ),
    path(
        "institutions/<uuid:institution_id>/role-assignments/<uuid:id>/",
        views.UserRoleAssignmentDetailView.as_view(),
        name="rbac-assignment-detail",
    ),

    # Institution role-change requests
    path(
        "institutions/<uuid:institution_id>/role-change-requests/",
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
        "vision/role-change-requests/<uuid:request_id>/decide/",
        views.VisionRoleChangeRequestDecisionView.as_view(),
        name="rbac-vision-role-change-decide",
    ),

    # Lock history
    path(
        "institutions/<uuid:institution_id>/role-lock-events/",
        views.RoleLockEventListView.as_view(),
        name="rbac-role-lock-events",
    ),

    # Effective permission cache (optional)
    path(
        "institutions/<uuid:institution_id>/permission-cache/<uuid:id>/",
        views.EffectivePermissionCacheDetailView.as_view(),
        name="rbac-permission-cache-detail",
    ),
]