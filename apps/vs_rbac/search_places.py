"""The roles and access screens the search bar can route to.

Roles and Field Access are core (:mod:`vs_rbac.capability_map`), so no place
names a capability. A school's screen and the platform's own answer to one
place each, under either key the endpoint accepts: the school frontend routes
``rbac.roles`` to the school's roles and the console to the platform's. The
permission catalogue and the platform's own role assignments are the
platform's alone.

It also registers two conditions other apps' places name. ``platform_staff``:
the screens any platform staff member opens without a key (account lockouts,
the to-do board) are gated by :class:`vs_rbac.permissions.IsVisionStaff`, and
the search bar asks that same class. ``several_branches``: a screen that
exists only at a tenant owning more than one branch, asked exactly as
:func:`vs_rbac.scoping.only_branch_id_or_several` answers it.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register, register_condition

from .views import FIELD_ACCESS_UPDATE_KEYS, FIELD_ACCESS_VIEW_KEYS, ROLE_VIEW_KEYS

#: Names the condition the platform's keyless staff screens require.
PLATFORM_STAFF = "platform_staff"


def is_platform_staff(*, request, tenant, branch, user) -> bool:
    """Whether the caller passes :class:`~vs_rbac.permissions.IsVisionStaff`."""
    from types import SimpleNamespace

    from .permissions import IsVisionStaff

    return IsVisionStaff().has_permission(request or SimpleNamespace(user=user), None)


register_condition(PLATFORM_STAFF, is_platform_staff)

#: Names the condition a screen existing only at a tenant with several branches requires.
SEVERAL_BRANCHES = "several_branches"


def owns_several_branches(*, request, tenant, branch, user) -> bool:
    """Whether *tenant* owns more than one branch; False with no tenant in scope."""
    from .scoping import only_branch_id_or_several

    if tenant is None:
        return False
    return only_branch_id_or_several(tenant) is None


register_condition(SEVERAL_BRANCHES, owns_several_branches)

register(
    Place(
        kind=ACTION, key="rbac.roles", label="Roles and permissions",
        description="Each role, what it may do, and who holds it.",
        keywords=("access", "permissions", "roles"),
        screen="rbac.roles", permission=tuple(ROLE_VIEW_KEYS),
    ),
    Place(
        kind=ACTION, key="rbac.field_access", label="Field access",
        description="Which fields each role may read or change.",
        keywords=("hide fields", "field permissions", "access"),
        screen="rbac.field_access",
        permission=(*FIELD_ACCESS_VIEW_KEYS, *FIELD_ACCESS_UPDATE_KEYS),
    ),
    Place(
        kind=ACTION, key="rbac.role_assignments", label="Platform user assignments",
        description="Which platform staff hold which platform role.",
        keywords=("roles", "access"),
        screen="rbac.role_assignments", permission="platform.roles.view",
    ),
    Place(
        kind=ACTION, key="rbac.permissions", label="Permission catalogue",
        description="Every permission, its module, resource and action, and what it depends on.",
        keywords=("permissions", "modules", "resources", "actions"),
        screen="rbac.permissions", permission="platform.permissions.view",
    ),
)
