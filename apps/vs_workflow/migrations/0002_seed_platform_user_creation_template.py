from django.db import migrations


DOCUMENT_TYPE = "PLATFORM_USER_CREATION"
TEMPLATE_CODE = "p-user-creation"


def seed_platform_user_creation_template(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")

    platform_tenant = Tenant.objects.filter(slug="codex", kind="PLATFORM").first()
    if platform_tenant is None:
        return

    template, _ = WorkflowTemplate.objects.get_or_create(
        tenant=platform_tenant,
        branch=None,
        document_type=DOCUMENT_TYPE,
        code=TEMPLATE_CODE,
        defaults={
            "name": "Platform user creation",
            "description": "Approval gate for inviting a new CX staff member.",
            "notification_events": {},
        },
    )
    WorkflowStage.objects.get_or_create(
        template=template,
        code="platform-admin-approval",
        defaults={
            "label": "Platform admin approval",
            "kind": "APPROVAL",
            "order": 1,
            "approver_source": "RBAC_PERMISSION",
            "approver_permission_key": "platform.team.create",
            "approver_scope": "PLATFORM",
            "advance_rule": "ANY",
            "quorum_count": 0,
            "on_rejection": "TERMINAL",
            # The requester is excluded by the resolver. On a new staging
            # install with only one super admin, zero remaining approvers makes
            # this stage skip and the invitation is finalised immediately.
            "skip_if_no_approvers": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            seed_platform_user_creation_template,
            migrations.RunPython.noop,
        ),
    ]
