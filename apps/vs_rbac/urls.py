from django.urls import path

from . import views

urlpatterns = [
    # -------------------------------------------------------------------------
    # Vision-owned Permission Registry
    # -------------------------------------------------------------------------
    path(
        "vision/permissions/",
        views.PermissionListCreateView.as_view(),
        name="rbac-permission-list-create",
    ),
    path(
        "vision/permissions/<str:key>/",
        views.PermissionDetailView.as_view(),
        name="rbac-permission-detail",
    ),
    path(
        "vision/permission-dependencies/",
        views.PermissionDependencyListCreateView.as_view(),
        name="rbac-permission-dependency-list-create",
    ),
    path(
        "vision/permission-dependencies/<int:id>/",
        views.PermissionDependencyDetailView.as_view(),
        name="rbac-permission-dependency-detail",
    ),

    # -------------------------------------------------------------------------
    # School-scoped Role Templates
    # -------------------------------------------------------------------------
    path(
        "schools/<slug:school_id>/roles/",
        views.RoleTemplateListCreateView.as_view(),
        name="rbac-role-list-create",
    ),
    path(
        "schools/<slug:school_id>/roles/<int:id>/",
        views.RoleTemplateDetailView.as_view(),
        name="rbac-role-detail",
    ),

    # -------------------------------------------------------------------------
    # School-scoped Role Assignments
    # -------------------------------------------------------------------------
    path(
        "schools/<slug:school_id>/role-assignments/",
        views.UserRoleAssignmentListCreateView.as_view(),
        name="rbac-assignment-list-create",
    ),
    path(
        "schools/<slug:school_id>/role-assignments/<int:id>/",
        views.UserRoleAssignmentDetailView.as_view(),
        name="rbac-assignment-detail",
    ),

    # -------------------------------------------------------------------------
    # School -> Vision Role Change Requests
    # -------------------------------------------------------------------------
    path(
        "schools/<slug:school_id>/role-change-requests/",
        views.SchoolRoleChangeRequestListCreateView.as_view(),
        name="rbac-role-change-request-list-create",
    ),

    # Vision review queue
    path(
        "vision/role-change-requests/",
        views.VisionRoleChangeRequestQueueView.as_view(),
        name="rbac-vision-role-change-queue",
    ),
    path(
        "vision/role-change-requests/<int:id>/",
        views.VisionRoleChangeRequestDetailView.as_view(),
        name="rbac-vision-role-change-detail",
    ),
    path(
        "vision/role-change-requests/<int:request_id>/decide/",
        views.VisionRoleChangeRequestDecisionView.as_view(),
        name="rbac-vision-role-change-decide",
    ),

    # -------------------------------------------------------------------------
    # Vision/Internal Platform Role Templates
    # -------------------------------------------------------------------------
    path(
        "platform/roles/",
        views.PlatformRoleTemplateListCreateView.as_view(),
        name="platform-rbac-role-list-create",
    ),
    path(
        "platform/roles/<uuid:id>/",
        views.PlatformRoleTemplateDetailView.as_view(),
        name="platform-rbac-role-detail",
    ),

    # -------------------------------------------------------------------------
    # Vision/Internal Platform Role Assignments
    # -------------------------------------------------------------------------
    path(
        "platform/role-assignments/",
        views.PlatformUserRoleAssignmentListCreateView.as_view(),
        name="platform-rbac-assignment-list-create",
    ),
    path(
        "platform/role-assignments/<uuid:id>/",
        views.PlatformUserRoleAssignmentDetailView.as_view(),
        name="platform-rbac-assignment-detail",
    ),

    # -------------------------------------------------------------------------
    # Vision/Internal Platform Role Change Requests
    # -------------------------------------------------------------------------
    path(
        "platform/role-change-requests/",
        views.PlatformRoleChangeRequestListCreateView.as_view(),
        name="platform-rbac-role-change-request-list-create",
    ),
    path(
        "platform/role-change-requests/<uuid:id>/",
        views.PlatformRoleChangeRequestDetailView.as_view(),
        name="platform-rbac-role-change-detail",
    ),
    path(
        "platform/role-change-requests/<uuid:request_id>/decide/",
        views.PlatformRoleChangeRequestDecisionView.as_view(),
        name="platform-rbac-role-change-decide",
    ),
]