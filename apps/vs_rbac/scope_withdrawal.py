"""Take a key back from the tenants that were already offered it.

Reclassifying a permission to ``PLATFORM`` settles who may hold it from now on.
It says nothing about the grants already written, and those sit on more surfaces
than the one that comes to mind: a tenant's own role, the prebuilt library every
new tenant copies its roles from, a tenant-scoped permission group, and a
personal ALLOW override.

A leftover row confers nothing - :func:`vs_rbac.evaluator.get_effective_permissions`
filters out every key that is not ``TENANT`` for a tenant that is not the
platform - so it stays invisible until the next *write* through that surface,
which the scope guard refuses. That is how one leftover row in the library
stopped schools being created at all: provisioning Finance Admin copies the
library's defaults into the new school's role, the guard refused
``finance.entity.create``, and the whole creation transaction went down with it.

So reclassification and withdrawal are one act, and this is the half that is
easy to forget. A migration that flips a scope calls :func:`withdraw_from_tenants`
for the keys it flips, inside the same ``RunPython``.

Scope and tenant kind are compared as literals rather than through the enums,
because this runs against the historical models a migration is handed, where an
enum that has moved on since would not describe the rows in front of it.
"""

#: Surfaces are read through ``apps.get_model`` when a migration supplies one.
_MODELS = (
    "Permission",
    "TenantRolePermission",
    "PrebuiltRolePermission",
    "GroupPermission",
    "UserPermissionOverride",
)

TENANT_SCOPE = "TENANT"
PLATFORM_SCOPE = "PLATFORM"
PLATFORM_KIND = "PLATFORM"
ALLOW = "ALLOW"


def _resolve(apps):
    if apps is None:
        from vs_rbac import models as rbac_models

        return {name: getattr(rbac_models, name) for name in _MODELS}
    return {name: apps.get_model("vs_rbac", name) for name in _MODELS}


def withdraw_from_tenants(keys=None, *, apps=None):
    """Delete every tenant-side grant of *keys*, and report what went.

    *keys* defaults to every key in the registry that is not ``TENANT``, which
    is exactly the set :func:`vs_rbac.models.platform_only_keys` refuses, so
    calling it with no arguments repairs whatever a past reclassification left
    behind rather than only the keys the caller happens to know about.

    What is deliberately kept:

    * **Platform tenants.** CX legitimately holds both scopes, so its own roles
      and overrides are left exactly as they are. The filter is "not the
      platform" rather than "a school", because an organization tenant is not a
      school and its stale rows break writes the same way.
    * **Platform-scoped permission groups.** Only CX can attach one, and
      :class:`vs_rbac.models.GroupPermission` allows a platform group to carry
      anything.
    * **DENY overrides.** A DENY is not a grant. Removing one hands the person
      back whatever it was taking away.

    Returns a dict keyed by surface, so a caller can say what it took back.
    """
    models = _resolve(apps)

    if keys is None:
        keys = list(
            models["Permission"].objects
            .exclude(scope=TENANT_SCOPE)
            .values_list("key", flat=True)
        )
    else:
        keys = [key for key in keys if key]
    if not keys:
        return {surface: 0 for surface in
                ("role_permissions", "prebuilt_defaults",
                 "group_memberships", "overrides")}

    role_permissions, _ = (
        models["TenantRolePermission"].objects
        .filter(permission_id__in=keys)
        .exclude(role__tenant__kind=PLATFORM_KIND)
        .delete()
    )
    # The library is a blueprint for tenant roles and nothing else, so a
    # platform key in it has no legitimate reading at all.
    prebuilt_defaults, _ = (
        models["PrebuiltRolePermission"].objects
        .filter(permission_id__in=keys)
        .delete()
    )
    group_memberships, _ = (
        models["GroupPermission"].objects
        .filter(permission_id__in=keys)
        .exclude(group__scope=PLATFORM_SCOPE)
        .delete()
    )
    overrides, _ = (
        models["UserPermissionOverride"].objects
        .filter(permission_id__in=keys, mode=ALLOW)
        .exclude(tenant__kind=PLATFORM_KIND)
        .delete()
    )

    return {
        "role_permissions": role_permissions,
        "prebuilt_defaults": prebuilt_defaults,
        "group_memberships": group_memberships,
        "overrides": overrides,
    }
