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
    # Vision-owned Permission Groups (shared across school + platform roles)
    # -------------------------------------------------------------------------
    path(
        "vision/permission-groups/",
        views.PermissionGroupListCreateView.as_view(),
        name="rbac-permission-group-list-create",
    ),
    path(
        "vision/permission-groups/<uuid:id>/",
        views.PermissionGroupDetailView.as_view(),
        name="rbac-permission-group-detail",
    ),

    # -------------------------------------------------------------------------
    # School-scoped Role Templates
    # -------------------------------------------------------------------------
    path(
        "schools/<slug:school_slug>/roles/",
        views.SchoolRoleTemplateListCreateView.as_view(),
        name="rbac-role-list-create",
    ),
    path(
        "schools/<slug:school_slug>/roles/<int:id>/",
        views.SchoolRoleTemplateDetailView.as_view(),
        name="rbac-role-detail",
    ),

    # -------------------------------------------------------------------------
    # School-scoped Role Assignments
    # -------------------------------------------------------------------------
    path(
        "schools/<slug:school_slug>/role-assignments/",
        views.SchoolUserRoleAssignmentListCreateView.as_view(),
        name="rbac-assignment-list-create",
    ),
    path(
        "schools/<slug:school_slug>/role-assignments/<int:id>/",
        views.SchoolUserRoleAssignmentDetailView.as_view(),
        name="rbac-assignment-detail",
    ),

    # -------------------------------------------------------------------------
    # School Role Change Requests (school-internal approval)
    # -------------------------------------------------------------------------
    path(
        "schools/<slug:school_slug>/role-change-requests/",
        views.SchoolRoleChangeRequestListCreateView.as_view(),
        name="rbac-role-change-request-list-create",
    ),

    # School-admin approval queue and decision endpoints
    path(
        "schools/<slug:school_slug>/role-change-requests/approval/",
        views.SchoolRoleChangeRequestApprovalQueueView.as_view(),
        name="rbac-role-change-approval-queue",
    ),
    path(
        "schools/<slug:school_slug>/role-change-requests/<int:id>/",
        views.SchoolRoleChangeRequestApprovalDetailView.as_view(),
        name="rbac-role-change-approval-detail",
    ),
    path(
        "schools/<slug:school_slug>/role-change-requests/<int:request_id>/decide/",
        views.SchoolRoleChangeRequestDecisionView.as_view(),
        name="rbac-role-change-decide",
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
        "platform/role-assignments/<int:id>/",
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
        "platform/role-change-requests/<int:id>/",
        views.PlatformRoleChangeRequestDetailView.as_view(),
        name="platform-rbac-role-change-detail",
    ),
    path(
        "platform/role-change-requests/<int:request_id>/decide/",
        views.PlatformRoleChangeRequestDecisionView.as_view(),
        name="platform-rbac-role-change-decide",
    ),

    # -------------------------------------------------------------------------
    # Super Admin Transfer
    # -------------------------------------------------------------------------
    path(
        "platform/transfer-super-admin/",
        views.TransferSuperAdminView.as_view(),
        name="platform-rbac-transfer-super-admin",
    ),
]