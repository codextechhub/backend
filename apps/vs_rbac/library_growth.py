"""A role that owns a whole module grows with it, in the tenants that copied it.

Finance Admin is the library role that carries every finance and payments key
(``PERMISSION_PREFIXES`` in ``seed_prebuilt_role_templates``), so the school with
one person running its money has one role to give them. A tenant receives a copy
of it when the tenant is created, and the copy is the tenant's own from then on.

The library keeps its promise as the module grows: the seed attaches every new
key to the library role. The copies did not. A key registered after a tenant was
created reached neither that tenant's Finance Admin nor anybody else there, so a
bursar holding the role every screen is built around could not see the buttons
of a feature shipped a week after their school opened, and nobody was told.

    Corona opens on 8 September with Finance Admin as the library has it. On
    1 October cutting a petty cash float ships behind
    ``finance.pettycash.return``. The library role gains it; Corona's copy does
    not, and Mrs Okafor, Corona's Finance Admin, never sees "Reduce float".

So a key the library role gains is granted to every tenant copy of it at the
moment it is gained (:func:`grant_to_copies`). That is the only moment, which is
what keeps a school's own decisions standing: a key the school later takes off
its copy deletes the grant row, and no later seed run puts it back, because the
library already held it. A key the school refused outright keeps its deny row,
which is never touched.

A copy is recognised by its key, under either spelling the library has used
(``finance_admin``, and ``finance-admin`` from the console adoption command).
Only tenant-scoped keys travel: a platform key never reaches a tenant's role.
The platform tenant's own roles are not copies and are left alone. Finance Admin
is a whole-tenant role, and the keys only open what each view's own branch
checks allow, so a copy a tenant has narrowed to one branch gains no reach.
"""
from __future__ import annotations

TENANT_SCOPE = "TENANT"
PLATFORM_KIND = "PLATFORM"


def copy_keys(prebuilt_key: str) -> set[str]:
    """Every tenant role key a copy of library role ``prebuilt_key`` has carried."""
    return {prebuilt_key, prebuilt_key.replace("_", "-")}


def _models(apps):
    if apps is None:
        from vs_rbac import models as rbac_models

        return rbac_models.Permission, rbac_models.TenantRoleTemplate, rbac_models.TenantRolePermission
    return (
        apps.get_model("vs_rbac", "Permission"),
        apps.get_model("vs_rbac", "TenantRoleTemplate"),
        apps.get_model("vs_rbac", "TenantRolePermission"),
    )


def tenant_copies(prebuilt_key: str, *, apps=None):
    """The tenant roles that are copies of library role ``prebuilt_key``."""
    _, Role, _ = _models(apps)
    return (
        Role.objects.filter(key__in=copy_keys(prebuilt_key), is_system_role=True)
        .exclude(tenant__kind=PLATFORM_KIND)
    )


def grant_to_copies(prebuilt_key: str, permission_keys, *, apps=None,
                    registered_after_role: bool = False) -> int:
    """Grant ``permission_keys`` to every tenant copy of ``prebuilt_key``. Returns rows added.

    A copy already holding a row for a key (a grant or a deny) keeps it. With
    ``registered_after_role`` a copy receives only the keys registered after the
    copy itself was created, which is the catch-up for keys the library gained
    before this rule existed: such a key cannot have been on the copy when the
    tenant got it, so its absence is the gap and not the tenant's decision.
    """
    Permission, _, RolePermission = _models(apps)
    keys = set(
        Permission.objects.filter(key__in=set(permission_keys), scope=TENANT_SCOPE)
        .values_list("key", flat=True)
    )
    if not keys:
        return 0
    created_at = dict(
        Permission.objects.filter(key__in=keys).values_list("key", "created_at")
    )
    added = 0
    for role in tenant_copies(prebuilt_key, apps=apps).only("id", "created_at"):
        held = set(
            RolePermission.objects.filter(role_id=role.pk, permission_id__in=keys)
            .values_list("permission_id", flat=True)
        )
        wanted = [
            key for key in sorted(keys - held)
            if not registered_after_role or created_at[key] > role.created_at
        ]
        RolePermission.objects.bulk_create(
            [RolePermission(role_id=role.pk, permission_id=key, granted=True) for key in wanted],
            ignore_conflicts=True,
        )
        added += len(wanted)
    return added
