from django.urls import path

from . import views

urlpatterns = [
    # -------------------------------------------------------------------------
    # Permission vocabulary — Module / Resource / Action
    # -------------------------------------------------------------------------
    path(
        "vision/permission-modules/",
        views.PermissionModuleListCreateView.as_view(),
        name="rbac-permission-module-list-create",
    ),
    path(
        "vision/permission-modules/<slug:name>/",
        views.PermissionModuleDetailView.as_view(),
        name="rbac-permission-module-detail",
    ),
    path(
        "vision/permission-resources/",
        views.PermissionResourceListCreateView.as_view(),
        name="rbac-permission-resource-list-create",
    ),
    path(
        "vision/permission-resources/<int:pk>/",
        views.PermissionResourceDetailView.as_view(),
        name="rbac-permission-resource-detail",
    ),
    path(
        "vision/permission-actions/",
        views.PermissionActionListCreateView.as_view(),
        name="rbac-permission-action-list-create",
    ),
    path(
        "vision/permission-actions/<slug:name>/",
        views.PermissionActionDetailView.as_view(),
        name="rbac-permission-action-detail",
    ),

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
    # Tenant-scoped Role Templates (roles addressed by per-tenant key)
    # -------------------------------------------------------------------------
    path(
        "tenants/<slug:tenant_slug>/roles/",
        views.TenantRoleTemplateListCreateView.as_view(),
        name="rbac-role-list-create",
    ),
    path(
        "tenants/<slug:tenant_slug>/roles/<slug:key>/",
        views.TenantRoleTemplateDetailView.as_view(),
        name="rbac-role-detail",
    ),

    # -------------------------------------------------------------------------
    # Tenant-scoped Role Assignments
    # -------------------------------------------------------------------------
    path(
        "tenants/<slug:tenant_slug>/role-assignments/",
        views.TenantUserRoleAssignmentListCreateView.as_view(),
        name="rbac-assignment-list-create",
    ),
    path(
        "tenants/<slug:tenant_slug>/role-assignments/<int:id>/",
        views.TenantUserRoleAssignmentDetailView.as_view(),
        name="rbac-assignment-detail",
    ),
    path(
        "tenants/<slug:tenant_slug>/role-assignments/<int:id>/revoke/",
        views.TenantUserRoleAssignmentRevokeView.as_view(),
        name="rbac-assignment-revoke",
    ),

    # -------------------------------------------------------------------------
    # Tenant Role Change Requests (tenant-internal approval)
    # -------------------------------------------------------------------------
    path(
        "tenants/<slug:tenant_slug>/role-change-requests/",
        views.TenantRoleChangeRequestListCreateView.as_view(),
        name="rbac-role-change-request-list-create",
    ),
    path(
        "tenants/<slug:tenant_slug>/role-change-requests/approval/",
        views.TenantRoleChangeRequestApprovalQueueView.as_view(),
        name="rbac-role-change-approval-queue",
    ),
    path(
        "tenants/<slug:tenant_slug>/role-change-requests/<int:id>/",
        views.TenantRoleChangeRequestApprovalDetailView.as_view(),
        name="rbac-role-change-approval-detail",
    ),
    path(
        "tenants/<slug:tenant_slug>/role-change-requests/<int:request_id>/decide/",
        views.TenantRoleChangeRequestDecisionView.as_view(),
        name="rbac-role-change-decide",
    ),

    # -------------------------------------------------------------------------
    # Super Admin Transfer (codex tenant)
    # -------------------------------------------------------------------------
    path(
        "platform/transfer-super-admin/",
        views.TransferSuperAdminView.as_view(),
        name="platform-rbac-transfer-super-admin",
    ),
]