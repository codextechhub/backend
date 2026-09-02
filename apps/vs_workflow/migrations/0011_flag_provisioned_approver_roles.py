"""Flag the approver roles provisioning already created, so they keep working.

``_users_for_role_key`` now resolves only roles marked ``is_system_role``. That
closes the escalation where anyone holding role-create could name a role "Payout
Approver", have its key slugify onto ``payout-approver``, and thereby put its
holders on the approver list for every payout the school raises.

Every approver role that exists today predates the flag and carries
``is_system_role = False``, so without this migration the new clause would
resolve nobody in any tenant and every approval ladder on the platform would
park at its first stage. This backfills them.

**Which rows, and why that split.** Only roles with ``created_by IS NULL``.
``ensure_approver_role`` - provisioning - creates without a creator;
``TenantRoleTemplateSerializer.create`` always stamps ``created_by`` with the
acting user. So the column separates "the platform made this" from "a person
typed this name into the roles screen", which is exactly the distinction the
flag is supposed to record. Blessing the second kind would migrate the
vulnerability forward under a new name.

At the time of writing that split costs nothing: across the reserved keys every
existing row is provisioned and none is API-created. A deployment where that is
not true has its API-created look-alikes left unflagged - they stop conferring
approval, which is the point - and ``manage.py audit_approver_roles`` lists them
so somebody can decide whether each was a coverage gap being filled honestly (in
which case re-run provisioning, or set the flag) or the thing this change exists
to stop.

Keys are read from the published stages rather than a hardcoded list, so this
migration reserves whatever the templates in *this* database actually name.
"""
from django.db import migrations


def flag_provisioned_approver_roles(apps, schema_editor):
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowStageApproverOverride = apps.get_model(
        "vs_workflow", "WorkflowStageApproverOverride",
    )
    WorkflowStageDynamicRule = apps.get_model("vs_workflow", "WorkflowStageDynamicRule")
    TenantRoleTemplate = apps.get_model("vs_rbac", "TenantRoleTemplate")

    keys = set()
    for model, fields in (
        (WorkflowStage, ("approver_role_key", "approver_role__key")),
        (WorkflowStageApproverOverride, ("approver_role_key",)),
        (WorkflowStageDynamicRule, ("role_key", "role__key")),
    ):
        for row in model.objects.values_list(*fields):
            keys.update(value for value in row if value)

    if not keys:
        return

    TenantRoleTemplate.objects.filter(
        key__in=sorted(keys), created_by__isnull=True, is_system_role=False,
    ).update(is_system_role=True)


def unflag(apps, schema_editor):
    """Deliberately a no-op.

    ``is_system_role`` is set by provisioning as well as by this migration, and
    nothing records which rows this run touched. Clearing them all on reverse
    would strip the flag from roles that were correctly marked when they were
    created, and the reverse of a data backfill must never destroy more than it
    restored. Reversing the schema is safe; the flag stays.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("vs_workflow", "0010_alter_workflowstage_skip_if_no_approvers"),
        ("vs_rbac", "0010_permissionregistryrevision"),
    ]

    operations = [
        migrations.RunPython(flag_provisioned_approver_roles, unflag),
    ]
