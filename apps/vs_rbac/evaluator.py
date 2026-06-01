"""
Runtime permission evaluator for RBAC.

Usage:
    from vs_rbac.evaluator import has_permission, get_effective_permissions

    if has_permission(request.user, "finance.invoice.approve", school=school):
        ...

    perms = get_effective_permissions(request.user, school=school)

Resolution chain:
    User
      └─ SchoolUserRoleAssignment (active)
           └─ SchoolRoleTemplate
                ├─ SchoolRolePermission        → direct grants / explicit denies
                └─ SchoolRoleGroup → PermissionGroup → GroupPermission  → grants

    Explicit denies from ``SchoolRolePermission`` override every grant source.
"""
from __future__ import annotations

from typing import Set

from .models import (
    GroupPermission,
    PlatformRoleGroup,
    PlatformRolePermission,
    PlatformUserRoleAssignment,
    SchoolRoleGroup,
    SchoolRolePermission,
    SchoolUserRoleAssignment,
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
    unioning direct ``SchoolRolePermission`` grants with permissions derived from any
    ``PermissionGroup`` attached to the role.

    For Vision staff: also resolves permissions from platform role assignments
    the same way.

    Explicit denies (``SchoolRolePermission.granted=False`` or
    ``PlatformRolePermission.granted=False``) override every grant source.
    """
    granted: Set[str] = set()
    denied: Set[str] = set()

    user_type = getattr(user, "user_type", "")

    # School-level roles
    if school is not None:
        active_role_ids = list(
            SchoolUserRoleAssignment.objects.filter(
                school=school,
                user=user,
                assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).values_list("role_id", flat=True)
        )

        if active_role_ids:
            # Direct role↔permission grants (and explicit denies)
            for perm_key, is_granted in SchoolRolePermission.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("permission_id", "granted"):
                if is_granted:
                    granted.add(perm_key)
                else:
                    denied.add(perm_key)

            # Permissions from attached groups
            group_ids = SchoolRoleGroup.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("group_id", flat=True)
            granted.update(_group_permission_keys(group_ids))

    # Platform-level roles (Vision staff)
    if user_type == "CX_STAFF":
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


def resolve_users_with_permission(school, branch, permission_key: str):
    """Return a QuerySet of active users holding permission_key in the given scope.

    Used by the workflow engine's approver resolver. Performs a reverse lookup
    through the RBAC role tables rather than evaluating per-user.

    School scope  → users with an active school role assignment at that school
                    whose role carries the permission.
    Platform scope (school=None, branch=None) → CX_STAFF users with an active
                    platform role assignment whose role carries the permission.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Permission keys that grant via a group
    group_ids_with_perm = GroupPermission.objects.filter(
        permission_id=permission_key,
    ).values_list("group_id", flat=True)

    if school is None:
        # Platform scope — look at PlatformUserRoleAssignment
        role_ids_direct = PlatformRolePermission.objects.filter(
            permission_id=permission_key, granted=True,
        ).values_list("role_id", flat=True)

        role_ids_via_groups = PlatformRoleGroup.objects.filter(
            group_id__in=group_ids_with_perm,
        ).values_list("role_id", flat=True)

        denied_role_ids = PlatformRolePermission.objects.filter(
            permission_id=permission_key, granted=False,
        ).values_list("role_id", flat=True)

        all_role_ids = (set(role_ids_direct) | set(role_ids_via_groups)) - set(denied_role_ids)

        user_ids = PlatformUserRoleAssignment.objects.filter(
            role_id__in=all_role_ids,
            assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).values_list("user_id", flat=True)

        return User.objects.filter(pk__in=user_ids, is_active=True, user_type="CX_STAFF")

    # School / branch scope — look at SchoolUserRoleAssignment
    role_ids_direct = SchoolRolePermission.objects.filter(
        permission_id=permission_key, granted=True,
    ).values_list("role_id", flat=True)

    role_ids_via_groups = SchoolRoleGroup.objects.filter(
        group_id__in=group_ids_with_perm,
    ).values_list("role_id", flat=True)

    denied_role_ids = SchoolRolePermission.objects.filter(
        permission_id=permission_key, granted=False,
    ).values_list("role_id", flat=True)

    all_role_ids = (set(role_ids_direct) | set(role_ids_via_groups)) - set(denied_role_ids)

    assignment_qs = SchoolUserRoleAssignment.objects.filter(
        school=school,
        role_id__in=all_role_ids,
        assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
    )
    if branch is not None:
        assignment_qs = assignment_qs.filter(user__branch=branch)

    user_ids = assignment_qs.values_list("user_id", flat=True)
    return User.objects.filter(pk__in=user_ids, is_active=True)
