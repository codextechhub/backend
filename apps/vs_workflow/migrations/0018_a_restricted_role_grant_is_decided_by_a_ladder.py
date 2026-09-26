"""A role grant carrying restricted permissions is decided by a ladder.

Granting somebody a role whose restricted permissions the granter does not hold
was refused outright. In a school with one administrator that refusal could
never be satisfied: nobody there holds the finance keys until a Finance Admin
exists, so nobody could seat the first one. The grant now raises a request of
document type ``rbac.role_grant``, and these are its ladders.

They are shaped exactly like the role-change ladders in
``0015_a_role_change_is_decided_by_a_ladder``, for the same reasons given there:
one tenant-less template for schools and one owned by the platform tenant, one
stage decided by ANY administrator, and a stage that parks rather than skipping
when nobody is on it, because approval here is the safety boundary.

The reverse leaves the rows in place, as 0015's does and for the reason its
``remove_the_ladders`` sets out.
"""
from django.db import migrations

DOCUMENT_TYPE = "rbac.role_grant"

TEMPLATES = [
    {
        "code": "role-grant",
        "name": "Restricted role grant",
        "description": (
            "Approval for giving somebody a role that carries restricted "
            "permissions the person granting it does not hold."
        ),
        "stage_code": "role-admin-approval",
        "stage_label": "Role administrator approval",
        "role_key": "school_admin",
        "scope": "SCHOOL",
    },
    {
        "code": "role-grant-platform",
        "name": "Platform restricted role grant",
        "description": (
            "Approval for giving somebody a platform role that carries "
            "restricted permissions the person granting it does not hold."
        ),
        "stage_code": "platform-admin-approval",
        "stage_label": "Platform admin approval",
        "role_key": "xvs_platform_admin",
        "scope": "PLATFORM",
        "platform_owned": True,
    },
]


def add_the_ladders(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")

    codex = Tenant.objects.filter(slug="codex", kind="PLATFORM").first()

    for spec in TEMPLATES:
        if spec.get("platform_owned") and codex is None:
            continue
        template, _ = WorkflowTemplate.objects.get_or_create(
            tenant=codex if spec.get("platform_owned") else None,
            branch=None,
            document_type=DOCUMENT_TYPE,
            code=spec["code"],
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "notification_events": {},
            },
        )
        WorkflowStage.objects.get_or_create(
            template=template,
            code=spec["stage_code"],
            defaults={
                "label": spec["stage_label"],
                "kind": "APPROVAL",
                "order": 1,
                "approver_source": "ROLE",
                "approver_role_key": spec["role_key"],
                "approver_scope": spec["scope"],
                "advance_rule": "ANY",
                "quorum_count": 0,
                "on_rejection": "TERMINAL",
                "skip_if_no_approvers": False,
            },
        )


def leave_the_ladders(apps, schema_editor):
    """Keep the templates; see ``0015``'s ``remove_the_ladders``."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0017_workflowinstance_document_details"),
    ]

    operations = [
        migrations.RunPython(add_the_ladders, leave_the_ladders),
    ]
