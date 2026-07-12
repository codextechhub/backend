"""Tenant-scoped RBAC evaluation.

Permission definitions and groups are global; every grant is reached through a
role and assignment owned by the effective user's active tenant.
"""
from __future__ import annotations

from typing import Set

from .models import (
    GroupPermission,
    TenantRoleGroup,
    TenantRolePermission,
    TenantUserRoleAssignment,
)


def _group_permission_keys(group_ids) -> Set[str]:
    if not group_ids:
        return set()
    return set(
        GroupPermission.objects.filter(group_id__in=group_ids).values_list(
            "permission_id", flat=True,
        )
    )


def _normalize_tenant(user, tenant=None, school=None):
    # ``school`` remains an internal migration bridge; public APIs do not accept
    # school as tenant context.
    if tenant is None and school is not None:
        tenant = getattr(school, "tenant", None)
    return tenant or getattr(user, "tenant", None)


def get_effective_permissions(user, tenant=None, branch=None, school=None) -> Set[str]:
    tenant = _normalize_tenant(user, tenant=tenant, school=school)
    if not user or not getattr(user, "is_authenticated", False) or tenant is None:
        return set()
    if getattr(user, "tenant_id", None) != tenant.pk:
        return set()

    cache_key = (tenant.pk, getattr(branch, "pk", None))
    cache = getattr(user, "_rbac_effective_perms", None)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    assignments = TenantUserRoleAssignment.objects.filter(
        tenant=tenant,
        user=user,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        role__status="ACTIVE",
    )
    if branch is None:
        assignments = assignments.filter(branch__isnull=True)
    else:
        assignments = assignments.filter(branch__isnull=True) | assignments.filter(branch=branch)
    role_ids = list(assignments.values_list("role_id", flat=True))

    granted, denied = set(), set()
    for key, is_granted in TenantRolePermission.objects.filter(
        role_id__in=role_ids,
    ).values_list("permission_id", "granted"):
        (granted if is_granted else denied).add(key)

    group_ids = TenantRoleGroup.objects.filter(role_id__in=role_ids).values_list(
        "group_id", flat=True,
    )
    granted.update(_group_permission_keys(group_ids))
    effective = granted - denied
    if cache is None:
        cache = {}
        user._rbac_effective_perms = cache
    cache[cache_key] = effective
    return effective


def has_permission(user, permission_key: str, tenant=None, branch=None, school=None) -> bool:
    return permission_key in get_effective_permissions(
        user, tenant=tenant, branch=branch, school=school,
    )


def has_any_permission(user, permission_keys, tenant=None, branch=None, school=None) -> bool:
    return bool(
        get_effective_permissions(user, tenant=tenant, branch=branch, school=school)
        & set(permission_keys)
    )


def has_all_permissions(user, permission_keys, tenant=None, branch=None, school=None) -> bool:
    return set(permission_keys).issubset(
        get_effective_permissions(user, tenant=tenant, branch=branch, school=school)
    )


def resolve_users_with_permission(tenant, branch, permission_key: str):
    """Return active users whose tenant assignment grants ``permission_key``."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    # Transitional workflow calls may still pass a School instance.
    tenant = getattr(tenant, "tenant", tenant)
    if tenant is None:
        return get_user_model().objects.none()

    group_ids = GroupPermission.objects.filter(permission_id=permission_key).values_list(
        "group_id", flat=True,
    )
    direct = TenantRolePermission.objects.filter(
        permission_id=permission_key, granted=True,
    ).values_list("role_id", flat=True)
    via_group = TenantRoleGroup.objects.filter(group_id__in=group_ids).values_list(
        "role_id", flat=True,
    )
    denied = TenantRolePermission.objects.filter(
        permission_id=permission_key, granted=False,
    ).values_list("role_id", flat=True)
    role_ids = (set(direct) | set(via_group)) - set(denied)

    assignments = TenantUserRoleAssignment.objects.filter(
        tenant=tenant,
        role_id__in=role_ids,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).filter(Q(branch__isnull=True) | Q(branch=branch))
    user_ids = assignments.values_list("user_id", flat=True)
    return get_user_model().objects.filter(
        pk__in=user_ids, is_active=True, tenant=tenant,
    )
