
# vs_config/views.py
#
# All views in vs_config delegate business logic to ConfigurationService
# and FlagService. Views are responsible for:
#   - Authentication (JWT via IsAuthenticated)
#   - Permission enforcement (has_platform_permission / has_institution_permission)
#   - Request parsing and response shaping
#   - Pagination
#   - Calling the correct service method
#
# Views never touch models directly. No ORM calls belong here.
#
# Pagination:
#   All list views use StandardResultsSetPagination (10 items / page).
#   Adjust page_size in settings if needed.

from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .models import ConfigurationKey
from .serializers import (
    ConfigurationKeyListSerializer,
    ConfigurationKeyDetailSerializer,
    ConfigurationKeyCreateSerializer,
    ConfigurationKeyUpdateSerializer,
    FlagToggleSerializer,
    BranchConfigOverrideSerializer,
    BranchOverrideBulkUpdateSerializer,
    ConfigurationChangeLogSerializer,
)
from .constants import ConfigPermissions, FLAG_REGISTRY
from .services.config import ConfigurationService
from .services.flags import FlagService
from .services.audit import write_audit_log, ConfigAuditActions

from vs_rbac.permissions import (
    IsAuthenticatedAndActive,
    HasRBACPermission,
    IsVisionSuperAdmin,
    IsSchoolAdmin,
)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class StandardResultsSetPagination(PageNumberPagination):
    page_size            = 10
    page_size_query_param = "page_size"
    max_page_size        = 100


# ---------------------------------------------------------------------------
# Helper: get branch from request (for branch-scoped views)
# ---------------------------------------------------------------------------

def _get_branch_from_request(request):
    """
    Returns the branch associated with the authenticated user.
    Adjust this to match how vs_schools/vs_users exposes branch on request.user.
    """
    return request.user.branch


# ---------------------------------------------------------------------------
# Global Configuration Key Views
# ---------------------------------------------------------------------------

class ConfigurationKeyListCreateView(APIView):
    """
    GET  /api/v1/config/keys/
         List all active global config keys.
         ?include_inactive=true includes soft-deleted keys (Super Admin only).

    POST /api/v1/config/keys/
         Create a new global config key. Super Admin only.

    docstring-name: Configuration keys
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.SYSTEM_MANAGE

    def get(self, request):
        include_inactive = (
            request.query_params.get("include_inactive", "").lower() == "true"
        )
        keys = ConfigurationService.list_active_keys(include_inactive=include_inactive)

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(keys, request)
        serializer = ConfigurationKeyListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        serializer = ConfigurationKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        config_key = ConfigurationService.create_key(
            key=serializer.validated_data["key"],
            value=serializer.validated_data["value"],
            description=serializer.validated_data["description"],
            actor=request.user,
        )

        return Response(
            ConfigurationKeyDetailSerializer(config_key).data,
            status=status.HTTP_201_CREATED,
        )


class ConfigurationKeyDetailView(APIView):
    """
    GET   /api/v1/config/keys/{key}/   Retrieve a single key by dot-notation name.
    PATCH /api/v1/config/keys/{key}/   Update value and/or description.
    DELETE /api/v1/config/keys/{key}/  Soft-delete. Blocked if referenced by overrides.

    docstring-name: Configuration keys
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.SYSTEM_MANAGE

    def _get_key(self, key_name):
        return get_object_or_404(ConfigurationKey, key=key_name)

    def get(self, request, key):
        config_key = self._get_key(key)
        return Response(ConfigurationKeyDetailSerializer(config_key).data)

    def patch(self, request, key):
        config_key = self._get_key(key)

        serializer = ConfigurationKeyUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated = ConfigurationService.update_key(
            config_key=config_key,
            value=serializer.validated_data.get("value"),
            description=serializer.validated_data.get("description"),
            actor=request.user,
        )

        return Response(ConfigurationKeyDetailSerializer(updated).data)

    def delete(self, request, key):
        config_key = self._get_key(key)
        ConfigurationService.soft_delete_key(config_key=config_key, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConfigurationKeyRestoreView(APIView):
    """
    POST /api/v1/config/keys/{key}/restore/
    Restore a soft-deleted configuration key.

    docstring-name: Restore a configuration key
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.SYSTEM_MANAGE

    def post(self, request, key):
        config_key = get_object_or_404(ConfigurationKey, key=key, is_active=False)
        restored = ConfigurationService.restore_key(config_key=config_key, actor=request.user)
        return Response(ConfigurationKeyDetailSerializer(restored).data)


# ---------------------------------------------------------------------------
# Feature Flag Views
# ---------------------------------------------------------------------------

class BranchFlagListView(APIView):
    """
    GET /api/v1/config/branches/{branch_id}/flags/
    Returns all flags in FLAG_REGISTRY annotated with branch state.
    Never-set flags appear with is_enabled=False.

    docstring-name: Branch feature flags
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.FLAGS_MANAGE

    def get(self, request, branch_id):
        from vs_schools.models import Branch
        branch = get_object_or_404(Branch, id=branch_id)

        flags = FlagService.get_all_flags_for_branch(branch)
        return Response({"flags": flags})


class BranchFlagToggleView(APIView):
    """
    PATCH /api/v1/config/branches/{branch_id}/flags/{flag_key}/
    Toggle a specific flag on or off for a branch.

    docstring-name: Toggle a branch feature flag
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.FLAGS_MANAGE

    def patch(self, request, branch_id, flag_key):
        from vs_schools.models import Branch
        branch = get_object_or_404(Branch, id=branch_id)

        serializer = FlagToggleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        flag = FlagService.toggle_flag(
            branch=branch,
            flag_key=flag_key,
            enable=serializer.validated_data["is_enabled"],
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )

        return Response({
            "flag_key":   flag.flag_key,
            "label":      FLAG_REGISTRY.get(flag.flag_key, flag.flag_key),
            "is_enabled": flag.is_enabled,
            "set_by":     str(flag.set_by_id) if flag.set_by_id else None,
            "set_at":     flag.set_at,
        })


class BranchFlagHistoryView(APIView):
    """
    GET /api/v1/config/branches/{branch_id}/flags/history/
    Returns paginated flag change history for a branch.
    Optional ?flag_key= to filter by a specific flag.

    docstring-name: Branch flag history
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.FLAGS_MANAGE

    def get(self, request, branch_id):
        from vs_schools.models import Branch
        branch = get_object_or_404(Branch, id=branch_id)

        flag_key = request.query_params.get("flag_key")
        history  = FlagService.get_flag_history(branch, flag_key=flag_key)

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(history, request)
        serializer = ConfigurationChangeLogSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


# ---------------------------------------------------------------------------
# Branch Self-Service Override Views
# ---------------------------------------------------------------------------

class BranchOverrideView(APIView):
    """
    GET   /api/v1/config/my-branch/overrides/
          List current override settings for the authenticated Branch Admin's branch.

    PATCH /api/v1/config/my-branch/overrides/
          Update one or more permitted override keys.
          Only keys in PERMITTED_SELF_SERVICE_KEYS are accepted.

    docstring-name: Branch configuration overrides
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.SELF_MANAGE

    def get(self, request):
        branch    = _get_branch_from_request(request)
        overrides = ConfigurationService.list_branch_overrides(branch)
        serializer = BranchConfigOverrideSerializer(overrides, many=True)
        return Response(serializer.data)

    def patch(self, request):
        branch = _get_branch_from_request(request)

        serializer = BranchOverrideBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        results = []
        for key, value in serializer.validated_data["overrides"].items():
            override = ConfigurationService.set_branch_override(
                branch=branch,
                key=key,
                value=value,
                actor=request.user,
            )
            results.append(override)

        return Response(
            BranchConfigOverrideSerializer(results, many=True).data
        )


class BranchOverrideHistoryView(APIView):
    """
    GET /api/v1/config/my-branch/overrides/history/
    Returns paginated change history for the authenticated Branch Admin's overrides.

    docstring-name: Branch override history
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.SELF_MANAGE

    def get(self, request):
        branch  = _get_branch_from_request(request)
        history = ConfigurationService.list_override_history(branch)

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(history, request)
        serializer = ConfigurationChangeLogSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


# ---------------------------------------------------------------------------
# Export View
# ---------------------------------------------------------------------------

class ConfigExportView(APIView):
    """
    GET /api/v1/config/export/
    Export all active global config keys and all branch flag states.
    Super Admin only. Action is logged to platform audit trail.

    docstring-name: Export configuration
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin | IsSchoolAdmin | HasRBACPermission]
    rbac_permission    = ConfigPermissions.SYSTEM_MANAGE
    
    def get(self, request):
        from vs_schools.models import Branch

        global_keys = ConfigurationService.list_active_keys(include_inactive=False)

        branch_flags = {}
        for branch in Branch.objects.filter(is_active=True):
            flags = FlagService.get_all_flags_for_branch(branch)
            branch_flags[branch.slug] = flags

        write_audit_log(
            actor=request.user,
            action=ConfigAuditActions.CONFIG_EXPORTED,
            target_type="ConfigExport",
            target_id="all",
            detail={"branch_count": len(branch_flags)},
        )

        return Response({
            "global_config": ConfigurationKeyListSerializer(global_keys, many=True).data,
            "branch_flags":  branch_flags,
        })
