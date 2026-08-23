"""Tenant-scoped RBAC evaluation.

Permission definitions and groups are global; every grant is reached through a
role and assignment owned by the effective user's active tenant.

Branch scope
------------
An assignment may be pinned to one :class:`vs_tenants.Branch`, or left whole
tenant (``branch IS NULL``). Reading that column correctly turns on the
distinction between two questions ``branch=None`` used to answer at once:

* *"the caller named no branch"* - what every permission gate means, and what
  :data:`ANY_BRANCH` now says explicitly. Every grant the user holds counts,
  whole tenant or branch pinned;
* *"the entity as a whole"* - the scope a document with ``branch IS NULL`` sits
  in, which only whole-tenant grants reach. That is what an explicit ``None``
  still means.

Conflating them is what made a branch-scoped grant confer nothing at all: the
holder was not narrowed to their branch, they were locked out. Which rows such
a holder may then *see* is a separate answer, given once by
:func:`vs_rbac.scoping.visible_branch_ids`, so access and visibility cannot
drift apart.
"""
from __future__ import annotations

from typing import Set, Tuple

from django.db.models import Q
from django.utils import timezone

from .models import (
    GroupPermission,
    PermissionScope,
    TenantRoleGroup,
    TenantRolePermission,
    TenantUserRoleAssignment,
    UserPermissionOverride,
    tenant_is_platform,
)


class _AnyBranch:
    """Type of the :data:`ANY_BRANCH` sentinel (a singleton, compared by identity)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "ANY_BRANCH"


#: "No branch was named, so do not narrow by branch."
#:
#: The default for every entry point here, and deliberately *not* ``None``:
#: ``None`` is a real, meaningful branch scope (the entity as a whole) and has
#: to keep meaning that for callers such as :func:`resolve_users_with_permission`,
#: which is asked who may act on a document that belongs to no branch.
ANY_BRANCH = _AnyBranch()


def _assignment_branch_q(branch) -> Q:
    """The branch condition on a role assignment, expressed in exactly one place.

    A whole-tenant grant (``branch IS NULL``) always counts - it is what "the
    whole tenant" means, and it is how everyone working today holds their access.
    A branch-pinned grant counts only while its branch is still in service, so a
    suspended, deactivated or closed site withdraws the access it conferred
    instead of leaving it hanging.

    The liveness test is written as a positive ``status IN (in service)`` rather
    than an exclusion: ``branch`` is nullable, and a negative filter across that
    join would take the whole-tenant grants down with it.
    """
    from vs_tenants.models import Branch

    live = Q(branch__status__in=Branch.IN_SERVICE_STATES)
    if branch is ANY_BRANCH:
        return Q(branch__isnull=True) | live
    if branch is None:
        return Q(branch__isnull=True)
    return Q(branch__isnull=True) | (Q(branch=branch) & live)


def _holdable_filter(tenant) -> dict:
    """Extra queryset filter dropping keys *tenant* is not allowed to hold.

    Defence in depth for the grant guards on the models: a row written before
    ``Permission.scope`` existed, restored from an old backup, or inserted by
    raw SQL still confers nothing, because evaluation refuses to return a key
    whose scope is not ``TENANT`` to a tenant that is not the platform. The
    filter is on an indexed column and costs nothing measurable.

    A platform tenant is unrestricted: CX legitimately holds both scopes.
    """
    if tenant_is_platform(tenant):
        return {}
    return {"permission__scope": PermissionScope.TENANT}


def _group_permission_keys(group_ids, tenant=None) -> Set[str]:
    if not group_ids:
        return set()
    qs = GroupPermission.objects.filter(group_id__in=group_ids)
    if tenant is not None:
        qs = qs.filter(**_holdable_filter(tenant))
    return set(qs.values_list("permission_id", flat=True))


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
    qs = _active_override_qs(tenant).filter(user=user, **_holdable_filter(tenant))
    for key, mode in qs.values_list("permission_id", "mode"):
        (denies if mode == UserPermissionOverride.Mode.DENY else allows).add(key)
    return allows, denies


def get_role_permissions(user, tenant=None, branch=ANY_BRANCH) -> Set[str]:
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
    role_ids = set(
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            user=user,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            role__status="ACTIVE",
        )
        .filter(_assignment_branch_q(branch))
        .values_list("role_id", flat=True)
    )

    granted, denied = set(), set()
    for key, is_granted in TenantRolePermission.objects.filter(
        role_id__in=role_ids, **_holdable_filter(tenant),
    ).values_list("permission_id", "granted"):
        (granted if is_granted else denied).add(key)

    group_ids = TenantRoleGroup.objects.filter(role_id__in=role_ids).values_list(
        "group_id", flat=True,
    )
    granted.update(_group_permission_keys(group_ids, tenant=tenant))
    return granted - denied


def get_effective_permissions(user, tenant=None, branch=ANY_BRANCH) -> Set[str]:
    """Everything *user* may do in *tenant* right now.

    Order of authority, later wins::

        (role_granted - role_denied) | user_allows - user_denies

    A personal DENY therefore beats a role grant, a group grant, and a personal
    ALLOW. The whole result (roles + overrides) is memoised on the user instance
    for the life of the request, so overrides cost one extra query per request,
    not one per permission check.

    Keys the tenant may not hold are never returned, whatever row grants them:
    see :func:`_holdable_filter`. That is a backstop, not the boundary - the
    grant models refuse to write such a row in the first place.
    """
    tenant = _normalize_tenant(user, tenant=tenant)
    if not user or not getattr(user, "is_authenticated", False) or tenant is None:
        return set()
    if getattr(user, "tenant_id", None) != tenant.pk:
        return set()

    # ``ANY_BRANCH`` keys itself rather than collapsing through ``getattr(...,
    # "pk", None)`` - it has no ``pk``, so folding it in would share one cache
    # entry with the explicit ``None`` scope, which answers a different question.
    cache_key = (
        tenant.pk,
        branch if branch is ANY_BRANCH else getattr(branch, "pk", None),
    )
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


def has_permission(user, permission_key: str, tenant=None, branch=ANY_BRANCH) -> bool:
    return permission_key in get_effective_permissions(
        user, tenant=tenant, branch=branch,
    )


def has_any_permission(user, permission_keys, tenant=None, branch=ANY_BRANCH) -> bool:
    return bool(
        get_effective_permissions(user, tenant=tenant, branch=branch)
        & set(permission_keys)
    )


def has_all_permissions(user, permission_keys, tenant=None, branch=ANY_BRANCH) -> bool:
    return set(permission_keys).issubset(
        get_effective_permissions(user, tenant=tenant, branch=branch)
    )


def resolve_users_with_permission(tenant, branch, permission_key: str):
    """Return active users whose tenant assignment grants ``permission_key``.

    ``branch`` here is the scope of the *work* (the document being routed), not a
    caller's context, so it is passed positionally and an explicit ``None`` keeps
    its meaning: a document belonging to the entity as a whole is approved by
    whole-tenant grant holders, never by somebody pinned to one site. Routing
    shares :func:`_assignment_branch_q` with the permission gate so a person this
    function nominates as an approver cannot be someone ``has_permission`` would
    then refuse.
    """
    from django.contrib.auth import get_user_model

    # Transitional workflow calls may still pass a School instance.
    tenant = getattr(tenant, "tenant", tenant)
    if tenant is None:
        return get_user_model().objects.none()

    # Routing must agree with the permission gate: nobody in a tenant that may
    # not hold this key can be nominated to act on it, however they were granted.
    if not tenant_is_platform(tenant):
        from .models import Permission

        holdable = Permission.objects.filter(
            key=permission_key, scope=PermissionScope.TENANT,
        ).exists()
        if not holdable:
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
    ).filter(_assignment_branch_q(branch))
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
