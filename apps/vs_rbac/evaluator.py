"""Tenant-scoped RBAC evaluation.

Permission definitions and groups are global; every grant is reached through a
role and assignment owned by the effective user's active tenant.
"""
from __future__ import annotations

from typing import Set, Tuple

from django.db.models import Q
from django.utils import timezone

from .models import (
    GroupPermission,
    TenantRoleGroup,
    TenantRolePermission,
    TenantUserRoleAssignment,
    UserPermissionOverride,
)


def _group_permission_keys(group_ids) -> Set[str]:
    if not group_ids:
        return set()
    return set(
        GroupPermission.objects.filter(group_id__in=group_ids).values_list(
            "permission_id", flat=True,
        )
    )


def _normalize_tenant(user, tenant=None):
    return tenant or getattr(user, "tenant", None)


def _active_override_qs(tenant=None):
    """Overrides that are in force right now (expiry is evaluated lazily).

    ``expires_at`` is never swept by a cron - an expired row simply stops
    matching this filter, so it stops applying the moment it lapses.
    """
    qs = UserPermissionOverride.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
    )
    if tenant is not None:
        qs = qs.filter(tenant=tenant)
    return qs


def get_user_override_keys(user, tenant) -> Tuple[Set[str], Set[str]]:
    """Return ``(allow_keys, deny_keys)`` currently in force for *user*.

    One indexed query (``tenant``, ``user``, ``expires_at``). Callers inside a
    request should prefer :func:`get_effective_permissions`, which folds this
    into the existing per-request cache.
    """
    allows: Set[str] = set()
    denies: Set[str] = set()
    for key, mode in _active_override_qs(tenant).filter(user=user).values_list(
        "permission_id", "mode",
    ):
        (denies if mode == UserPermissionOverride.Mode.DENY else allows).add(key)
    return allows, denies


def get_role_permissions(user, tenant=None, branch=None) -> Set[str]:
    """Role-derived permissions only - personal overrides are NOT applied.

    Used by the overrides API to answer "does a role currently grant this key?"
    for each override row. Never use it for authorisation.
    """
    tenant = _normalize_tenant(user, tenant=tenant)
    if not user or not getattr(user, "is_authenticated", False) or tenant is None:
        return set()
    if getattr(user, "tenant_id", None) != tenant.pk:
        return set()
    return _role_permission_keys(user, tenant, branch)


def _role_permission_keys(user, tenant, branch) -> Set[str]:
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
    return granted - denied


def get_effective_permissions(user, tenant=None, branch=None) -> Set[str]:
    """Everything *user* may do in *tenant* right now.

    Order of authority, later wins::

        (role_granted - role_denied) | user_allows - user_denies

    A personal DENY therefore beats a role grant, a group grant, and a personal
    ALLOW. The whole result (roles + overrides) is memoised on the user instance
    for the life of the request, so overrides cost one extra query per request,
    not one per permission check.
    """
    tenant = _normalize_tenant(user, tenant=tenant)
    if not user or not getattr(user, "is_authenticated", False) or tenant is None:
        return set()
    if getattr(user, "tenant_id", None) != tenant.pk:
        return set()

    cache_key = (tenant.pk, getattr(branch, "pk", None))
    cache = getattr(user, "_rbac_effective_perms", None)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    effective = _role_permission_keys(user, tenant, branch)
    allows, denies = get_user_override_keys(user, tenant)
    effective = (effective | allows) - denies

    if cache is None:
        cache = {}
        user._rbac_effective_perms = cache
    cache[cache_key] = effective
    return effective


def has_permission(user, permission_key: str, tenant=None, branch=None) -> bool:
    return permission_key in get_effective_permissions(
        user, tenant=tenant, branch=branch,
    )


def has_any_permission(user, permission_keys, tenant=None, branch=None) -> bool:
    return bool(
        get_effective_permissions(user, tenant=tenant, branch=branch)
        & set(permission_keys)
    )


def has_all_permissions(user, permission_keys, tenant=None, branch=None) -> bool:
    return set(permission_keys).issubset(
        get_effective_permissions(user, tenant=tenant, branch=branch)
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
    user_ids = set(assignments.values_list("user_id", flat=True))

    # Personal overrides are part of the effective set, so routing must honour
    # them too - otherwise a user denied the key would still be picked as an
    # approver/recipient while has_permission() says no.
    overrides = _active_override_qs(tenant).filter(permission_id=permission_key)
    user_ids |= set(
        overrides.filter(mode=UserPermissionOverride.Mode.ALLOW).values_list("user_id", flat=True)
    )
    user_ids -= set(
        overrides.filter(mode=UserPermissionOverride.Mode.DENY).values_list("user_id", flat=True)
    )

    return get_user_model().objects.filter(
        pk__in=user_ids, is_active=True, tenant=tenant,
    )
