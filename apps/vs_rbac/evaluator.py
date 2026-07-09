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


# Support permission-group expansion for both school and platform roles.
def _group_permission_keys(group_ids) -> Set[str]:
    """Return the set of permission keys belonging to the given group ids."""
    if not group_ids:
        return set()
    return set(
        GroupPermission.objects.filter(group_id__in=group_ids).values_list(
            "permission_id", flat=True
        )
    )


# Resolve the effective permission set used by API guards, FLS, and workflows.
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

    The result is memoised on the user instance (keyed by school). User
    objects are re-fetched from the DB on every request, so the cache is
    naturally request-scoped — permission changes still apply on the next
    request, but a single request no longer pays 4-6 queries per checked key.
    """
    cache_key = getattr(school, "pk", None) if school is not None else None
    cache = getattr(user, "_rbac_effective_perms", None)
    if cache is not None and cache_key in cache:
        return cache[cache_key]  # Reuse the request-local snapshot for repeated permission checks.

    granted: Set[str] = set()
    denied: Set[str] = set()

    user_type = getattr(user, "user_type", "")

    # School roles only apply inside the resolved tenant boundary.
    if school is not None:
        active_role_ids = list(
            SchoolUserRoleAssignment.objects.filter(
                school=school,
                user=user,
                assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).values_list("role_id", flat=True)
        )

        if active_role_ids:
            # Direct role permissions carry both grants and explicit role-level revokes.
            for perm_key, is_granted in SchoolRolePermission.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("permission_id", "granted"):
                if is_granted:
                    granted.add(perm_key)
                else:
                    denied.add(perm_key)

            # Group grants are reusable bundles; direct denies below still override them.
            group_ids = SchoolRoleGroup.objects.filter(
                role_id__in=active_role_ids,
            ).values_list("group_id", flat=True)
            granted.update(_group_permission_keys(group_ids))

    # Platform roles grant global Vision permissions and never depend on a school.
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

    # Denies win so a role can remove a sensitive permission inherited from a group.
    effective = granted - denied

    try:
        if cache is None:
            cache = {}
            user._rbac_effective_perms = cache
        cache[cache_key] = effective
    except AttributeError:
        pass  # exotic user objects without settable attrs — just skip caching

    return effective


# Check one RBAC key for permission classes and service guards.
def has_permission(user, permission_key: str, school=None) -> bool:
    """Check whether a user holds a specific permission."""
    return permission_key in get_effective_permissions(user, school=school)


# Check whether any key in an endpoint's allowed operation set is present.
def has_any_permission(user, permission_keys: list[str], school=None) -> bool:
    """Check whether a user holds at least one of the given permissions."""
    effective = get_effective_permissions(user, school=school)
    return bool(effective & set(permission_keys))


# Check permission bundles where every grant is required.
def has_all_permissions(user, permission_keys: list[str], school=None) -> bool:
    """Check whether a user holds all of the given permissions."""
    effective = get_effective_permissions(user, school=school)
    return set(permission_keys).issubset(effective)


# Resolve workflow approver candidates from role assignments without per-user evaluation.
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

    # Include group-derived grants so approval routing matches runtime permission checks.
    group_ids_with_perm = GroupPermission.objects.filter(
        permission_id=permission_key,
    ).values_list("group_id", flat=True)

    if school is None:
        # Platform approval queues are limited to active Vision staff assignments.
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

    # School approval queues honor tenant scope, with branch narrowing when supplied.
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
