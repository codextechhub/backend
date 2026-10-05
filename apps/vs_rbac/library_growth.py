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

The same rule holds for every library role a module seed attaches defaults to,
whichever seed owns the key: :func:`attach_defaults` attaches them to the
library role and grants only the keys it newly attached. The library link is
therefore the record of which defaults have already been offered to the copies.
A seed that granted its whole default list to every copy on each run put back,
every time, the keys a tenant had taken off its own roles:

    Bright Star takes ``exports.file.download`` off its Branch Admin role, so
    branch staff stop taking files home. The next deploy runs the seeds and the
    key is back, and nobody at Bright Star is told.

Branch-scoped library roles are copied once per branch with the branch's pk on
the key (``branch_admin-37``), so a seed whose keys belong on those copies asks
for them with ``branch_copies``. ``tenant_kind`` narrows the copies to one kind
of tenant for a seed whose keys only make sense there.

Resetting the library (``seed_prebuilt_role_templates --reset``, a development
tool) deletes every link, so the next run of each module seed offers its whole
list again.
"""
from __future__ import annotations

import re

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


def tenant_copies(prebuilt_key: str, *, apps=None, branch_copies: bool = False,
                  tenant_kind: str | None = None):
    """The tenant roles that are copies of library role ``prebuilt_key``.

    ``branch_copies`` adds the per-branch copies (``<key>-<branch pk>``), and
    nothing else whose key merely begins the same way. ``tenant_kind`` keeps
    only the copies held by tenants of that kind.
    """
    from django.db.models import Q

    _, Role, _ = _models(apps)
    spellings = copy_keys(prebuilt_key)
    match = Q(key__in=spellings)
    if branch_copies:
        for spelling in spellings:
            match |= Q(key__regex=rf"^{re.escape(spelling)}-\d+$")
    copies = Role.objects.filter(match, is_system_role=True).exclude(tenant__kind=PLATFORM_KIND)
    if tenant_kind is not None:
        copies = copies.filter(tenant__kind=tenant_kind)
    return copies


def grant_to_copies(prebuilt_key: str, permission_keys, *, apps=None,
                    registered_after_role: bool = False, branch_copies: bool = False,
                    tenant_kind: str | None = None) -> int:
    """Grant ``permission_keys`` to every tenant copy of ``prebuilt_key``. Returns rows added.

    A copy already holding a row for a key (a grant or a deny) keeps it. With
    ``registered_after_role`` a copy receives only the keys registered after the
    copy itself was created, which is the catch-up for keys the library gained
    before this rule existed: such a key cannot have been on the copy when the
    tenant got it, so its absence is the gap and not the tenant's decision.
    ``branch_copies`` and ``tenant_kind`` choose the copies as
    :func:`tenant_copies` does.
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
    copies = tenant_copies(
        prebuilt_key, apps=apps, branch_copies=branch_copies, tenant_kind=tenant_kind,
    )
    for role in copies.only("id", "created_at"):
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


def attach_defaults(prebuilt, permission_keys, *, branch_copies: bool = False,
                    tenant_kind: str | None = None) -> tuple[list[str], int]:
    """Attach ``permission_keys`` to library role ``prebuilt`` and grow its copies.

    Returns the keys newly attached and the grant rows added to tenant copies.
    Only a newly attached key is granted to the copies, at this one moment; a
    key the library already held was offered to them when it was attached, so
    its absence from a copy now is that tenant's decision and stays. A key with
    no ``Permission`` row is skipped, so a seed never attaches a key it failed
    to register.
    """
    from vs_rbac.models import Permission, PrebuiltRolePermission

    known = set(
        Permission.objects.filter(key__in=set(permission_keys)).values_list("key", flat=True)
    )
    attached = []
    for key in sorted(known):
        _, created = PrebuiltRolePermission.objects.get_or_create(
            prebuilt_role=prebuilt, permission_id=key,
        )
        if created:
            attached.append(key)
    grown = grant_to_copies(
        prebuilt.key, attached, branch_copies=branch_copies, tenant_kind=tenant_kind,
    ) if attached else 0
    return attached, grown
