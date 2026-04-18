"""
Runtime permission evaluator for RBAC.

Usage:
    from vs_rbac.evaluator import has_permission, get_effective_permissions

    if has_permission(request.user, "finance.invoice.approve", school=school):
        ...

    perms = get_effective_permissions(request.user, school=school)

Resolution chain:
    User
      └─ UserRoleAssignment (active)
           └─ RoleTemplate
                ├─ RolePermission        → direct grants / explicit denies
                └─ RoleGroup → PermissionGroup → GroupPermission  → grants

    Explicit denies from ``RolePermission`` override every grant source.
"""
from __future__ import annotations

from typing import Set

from .models import (
    GroupPermission,
    PlatformRoleGroup,
    PlatformRolePermission,
    PlatformUserRoleAssignment,
    RoleGroup,
    RolePermission,
    UserRoleAssignment,
)


def _group_permission_keys(group_ids) -> Set[str]:
    """Return the set of permission keys belonging to the given group ids."""
    if not group_ids:
        return set()
    return set(
        GroupPermission.objects.filter(group_id__in=group_ids).values_list(
            "permission_id", flat=True
        )
    )


def get_effective_permissions(user, school=None) -> Set[str]:
    """
    Compute the full set of granted permission keys for a user.

    For school-scoped users: resolves permissions from school role assignments,
    unioning direct ``RolePermission`` grants with permissions derived from any
    ``PermissionGroup`` attached to the role.

    For Vision staff: also resolves permissions from platform role assignments
    the same way.

    Explicit denies (``RolePermission.granted=False`` or
    ``PlatformRolePermission.granted=False``) override every grant source.
    """
    granted: Set[str] = set()
    denied: Set[str] = set()

    user_type = getattr(user, "user_type", "")

    # School-level roles
    if school is not None:
        active_role_ids = list(
            UserRoleAssignment.objects.filter(
                school=school,
                user=user,
                assignment_status=UserRoleAssignment.AssignmentStatus.ACTIVE,
            ).values_list("role_id", flat=True)
        )

        if active_role_ids:
            # Direct role↔permission grants (and explicit denies)
            for perm_key, is_granted in RolePermission.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("permission_id", "granted"):
                if is_granted:
                    granted.add(perm_key)
                else:
                    denied.add(perm_key)

            # Permissions from attached groups
            group_ids = RoleGroup.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("group_id", flat=True)
            granted.update(_group_permission_keys(group_ids))

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

            group_ids = PlatformRoleGroup.objects.filter(
                role_id__in=active_platform_role_ids,
            ).values_list("group_id", flat=True)
            granted.update(_group_permission_keys(group_ids))

    # Explicit denies win over grants
    return granted - denied


def has_permission(user, permission_key: str, school=None) -> bool:
    """Check whether a user holds a specific permission."""
    return permission_key in get_effective_permissions(user, school=school)


def has_any_permission(user, permission_keys: list[str], school=None) -> bool:
    """Check whether a user holds at least one of the given permissions."""
    effective = get_effective_permissions(user, school=school)
    return bool(effective & set(permission_keys))


def has_all_permissions(user, permission_keys: list[str], school=None) -> bool:
    """Check whether a user holds all of the given permissions."""
    effective = get_effective_permissions(user, school=school)
    return set(permission_keys).issubset(effective)
