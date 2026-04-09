"""
Runtime permission evaluator for RBAC.

Usage:
    from vs_rbac.evaluator import has_permission, get_effective_permissions

    if has_permission(request.user, "finance.invoice.approve", institution=institution):
        ...

    perms = get_effective_permissions(request.user, institution=institution)
"""
from __future__ import annotations

from typing import Set

from django.db.models import Q

from .models import (
    RolePermission,
    UserRoleAssignment,
    PlatformRolePermission,
    PlatformUserRoleAssignment,
)


def get_effective_permissions(user, institution=None) -> Set[str]:
    """
    Compute the full set of granted permission keys for a user.

    For institution-scoped users: resolves permissions from institution role assignments.
    For Vision staff: also resolves permissions from platform role assignments.

    Explicit denies (granted=False) override grants across all assigned roles.
    """
    granted = set()
    denied = set()

    user_type = getattr(user, "user_type", "")

    # Institution-level roles
    if institution is not None:
        active_role_ids = list(
            UserRoleAssignment.objects.filter(
                institution=institution,
                user=user,
                assignment_status=UserRoleAssignment.AssignmentStatus.ACTIVE,
            ).values_list("role_id", flat=True)
        )

        if active_role_ids:
            for perm_key, is_granted in RolePermission.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("permission_id", "granted"):
                if is_granted:
                    granted.add(perm_key)
                else:
                    denied.add(perm_key)

    # Platform-level roles (Vision staff)
    if user_type == "VS_STAFF":
        active_platform_role_ids = list(
            PlatformUserRoleAssignment.objects.filter(
                user=user,
                assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).values_list("role_id", flat=True)
        )

        if active_platform_role_ids:
            for perm_key, is_granted in PlatformRolePermission.objects.filter(
                role_id__in=active_platform_role_ids,
            ).values_list("permission_id", "granted"):
                if is_granted:
                    granted.add(perm_key)
                else:
                    denied.add(perm_key)

    # Explicit denies win over grants
    return granted - denied


def has_permission(user, permission_key: str, institution=None) -> bool:
    """Check whether a user holds a specific permission."""
    return permission_key in get_effective_permissions(user, institution=institution)


def has_any_permission(user, permission_keys: list[str], institution=None) -> bool:
    """Check whether a user holds at least one of the given permissions."""
    effective = get_effective_permissions(user, institution=institution)
    return bool(effective & set(permission_keys))


def has_all_permissions(user, permission_keys: list[str], institution=None) -> bool:
    """Check whether a user holds all of the given permissions."""
    effective = get_effective_permissions(user, institution=institution)
    return set(permission_keys).issubset(effective)
