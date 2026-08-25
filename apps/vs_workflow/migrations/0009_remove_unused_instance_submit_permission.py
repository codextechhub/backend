"""Retire ``workflow.instance.submit``, which no longer gates anything.

The key existed for one endpoint, the generic ``POST /v1/workflow/instances/``.
That endpoint took a content type and an object id and loaded the document with
the model's ordinary manager, so it could load any tenant's row; it has been
removed rather than narrowed, because the engine cannot answer which documents a
caller may address. Submission happens through each module's own endpoint under
that module's own key - ``finance.creditnote.submit``,
``procurement.requisition.submit`` - so nothing reads this one.

A key that grants nothing is worse than no key: it reads on a role screen as
though it confers the ability to submit, and an administrator who grants it will
believe they have given something.

Grants go first, then the key. ``TenantRolePermission`` and ``GroupPermission``
cascade, but ``UserPermissionOverride`` PROTECTs, so its rows must be cleared by
hand or the delete raises. ``TenantRoleChangeDeltaItem`` also PROTECTs and is
deliberately **not** cleared: it is the record of an approval ceremony that
really happened, and deleting it would rewrite that history to tidy up a
permission. Where such a row exists the key is deactivated instead of deleted,
which leaves it just as ungrantable while the audit record keeps its referent.

Reverse recreates the key inactive rather than restoring grants. Rolling back
should not silently re-award a permission somebody deliberately removed, and the
seeder no longer creates it, so an active row would drift from a fresh install.
"""
from django.db import migrations
from django.db.models import F

PERMISSION_KEY = "workflow.instance.submit"


def retire_submit_permission(apps, schema_editor):
    GroupPermission = apps.get_model("vs_rbac", "GroupPermission")
    Permission = apps.get_model("vs_rbac", "Permission")
    TenantRoleChangeDeltaItem = apps.get_model("vs_rbac", "TenantRoleChangeDeltaItem")
    TenantRoleGroup = apps.get_model("vs_rbac", "TenantRoleGroup")
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    TenantRoleTemplate = apps.get_model("vs_rbac", "TenantRoleTemplate")
    UserPermissionOverride = apps.get_model("vs_rbac", "UserPermissionOverride")

    permission = Permission.objects.filter(key=PERMISSION_KEY).first()
    if permission is None:
        return  # Never seeded here, or already retired.

    # Every role whose contents are about to change, collected before the
    # deletes so the version bump below can find them.
    role_ids = set(
        TenantRolePermission.objects.filter(permission_id=PERMISSION_KEY)
        .values_list("role_id", flat=True)
    )
    group_ids = list(
        GroupPermission.objects.filter(permission_id=PERMISSION_KEY)
        .values_list("group_id", flat=True).distinct()
    )
    role_ids |= set(
        TenantRoleGroup.objects.filter(group_id__in=group_ids)
        .values_list("role_id", flat=True)
    )

    GroupPermission.objects.filter(permission_id=PERMISSION_KEY).delete()
    TenantRolePermission.objects.filter(permission_id=PERMISSION_KEY).delete()
    UserPermissionOverride.objects.filter(permission_id=PERMISSION_KEY).delete()

    if role_ids:
        # A role's contents changed, so anything caching by version must refetch.
        TenantRoleTemplate.objects.filter(id__in=role_ids).update(version=F("version") + 1)

    if TenantRoleChangeDeltaItem.objects.filter(permission_id=PERMISSION_KEY).exists():
        Permission.objects.filter(key=PERMISSION_KEY).update(is_active=False)
    else:
        Permission.objects.filter(key=PERMISSION_KEY).delete()


def restore_inactive_permission(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionAction = apps.get_model("vs_rbac", "PermissionAction")
    PermissionModule = apps.get_model("vs_rbac", "PermissionModule")
    PermissionResource = apps.get_model("vs_rbac", "PermissionResource")

    if Permission.objects.filter(key=PERMISSION_KEY).exists():
        return

    module = PermissionModule.objects.filter(name="workflow").first()
    action = PermissionAction.objects.filter(name="submit").first()
    if module is None or action is None:
        return  # Nothing to hang it off; a fresh install never had the key.
    resource = PermissionResource.objects.filter(module=module, name="instance").first()
    if resource is None:
        return

    Permission.objects.create(
        key=PERMISSION_KEY, module=module, resource=resource, action=action,
        description="Submit a document for workflow approval (retired).",
        is_restricted=False, sensitivity_level="NORMAL", is_active=False,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0009_remove_restricted_grant_bypasses"),
        ("vs_workflow", "0008_retarget_branch_to_vs_tenants"),
    ]

    operations = [
        migrations.RunPython(retire_submit_permission, restore_inactive_permission),
    ]
