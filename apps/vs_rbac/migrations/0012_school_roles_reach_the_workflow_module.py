"""A school can see and shape its own approval rules.

``seed_workflow_permissions`` registers the workflow keys and grants them to
``xvs_super_admin`` and ``xvs_platform_admin`` - the two platform roles - and to
nothing else. That was right while the workflow engine was a CodeX surface. It
stopped being right the moment a school was given screens for its own approvals,
and the gap is total rather than partial: no school role held a single
``workflow.*`` key, so a head teacher could not read the rule that decides who
signs off her own payouts, let alone change it.

The split follows who is answerable for what:

* **School Admin** gets the manage keys. Deciding who approves the school's
  money is the head's call, and there is nobody else in a school to make it.
* **Finance Admin** and **Procurement Admin** get the read keys for their own
  documents' rules. Seeing which ladder governs a purchase order is part of
  running procurement; changing it is not.
* **Branch Admin** gets instance visibility only, because a branch admin
  answers questions about documents in flight and configures nothing.

``group.manage`` and ``template.manage`` are restricted keys. That bars them
from a permission group, not from a role: ``TenantRolePermission`` checks tenant
scope only, and this writes those rows directly. The restriction exists so a key
cannot be handed out by attaching a group to a role, which is not what is
happening here.
"""
from django.db import migrations

GRANTS = {
    "school_admin": [
        "workflow.template.view", "workflow.template.manage",
        "workflow.group.view", "workflow.group.manage",
        "workflow.instance.view", "workflow.instance.cancel",
    ],
    "finance-admin": ["workflow.template.view", "workflow.instance.view",
                      "workflow.group.view"],
    "procurement-admin": ["workflow.template.view", "workflow.instance.view",
                          "workflow.group.view"],
    "branch_admin": ["workflow.instance.view"],
}


def grant(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    TenantRoleTemplate = apps.get_model("vs_rbac", "TenantRoleTemplate")
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")

    known = set(
        Permission.objects.filter(
            key__in={k for keys in GRANTS.values() for k in keys},
        ).values_list("key", flat=True)
    )

    for role_key, keys in GRANTS.items():
        roles = TenantRoleTemplate.objects.filter(
            key=role_key, tenant__kind="SCHOOL",
        )
        for role in roles:
            for permission_key in keys:
                # A key the registry does not have yet is skipped rather than
                # created: the seeder owns the registry, and inventing a row
                # here would give it a description nobody wrote.
                if permission_key not in known:
                    continue
                TenantRolePermission.objects.get_or_create(
                    role=role, permission_id=permission_key,
                    defaults={"granted": True},
                )


def revoke(apps, schema_editor):
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    for role_key, keys in GRANTS.items():
        TenantRolePermission.objects.filter(
            role__key=role_key, role__tenant__kind="SCHOOL",
            permission_id__in=keys,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0011_finance_and_procurement_admin_are_codex_roles"),
    ]
    operations = [migrations.RunPython(grant, revoke)]
