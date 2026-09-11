"""A role change is approved the way every other consequential act is.

Role permission changes had an approval rule of their own: a grant ceiling that
let somebody approve only the restricted keys they already held themselves.
It refused the wrong people. A school whose only holder of
``school.roles.approve`` is the head teacher could raise a request for a key she
did not have and then never close it, because approving it required already
holding it. The only ways out were CodeX reaching into the tenant by hand, or
nobody ever getting the permission.

This gives the document type a ladder instead. The engine resolves approvers
from a role, records who voted and when, supports delegation and reversal, and
lets a school with two administrators have the two-person review the ceiling was
reaching for but could not produce.

**Two templates, because the model serves two kinds of tenant.** The same
``TenantRoleChangeRequest`` carries a school changing its own roles and CodeX
changing the platform's, and the role that approves is not the same in both. One
template with a condition on the tenant kind could route both, and would mean a
school editing its own ladder was editing a row that also encodes CodeX's.

They are scoped differently for the same reason. The school template is
tenant-less, so one row serves every school through the branch → tenant →
platform cascade and a school that wants its own still overrides by creating
one. The platform template belongs to the platform tenant, because the template
list shows a tenant its own templates AND every global one - a global platform
template would put "Platform role permission change" on the screen of every
school, naming a role key no school has. Scoping it to codex takes it off their
screen without taking it out of the cascade, which finds a tenant-scoped
template before it looks at the global ones.

**One stage, ANY, and it must not skip.** ANY because a school that staffs two
administrators should not need both to agree before a role can be edited. Not
skipping because approval here IS the safety boundary: a stage that waved itself
through would grant restricted permissions with nobody looking, which is worse
than the ceiling this replaces. A stage with nobody on it parks, and the
requester being eligible to decide their own is what keeps that from happening
in a school of one administrator - see
``BaseWorkflowHandler.allows_requester_self_approval``.
"""
from django.db import migrations

DOCUMENT_TYPE = "rbac.role_change"

TEMPLATES = [
    {
        "code": "role-change",
        "name": "Role permission change",
        "description": (
            "Approval for adding or removing a role's permissions. Restricted "
            "permissions cannot be granted by editing a role directly, so this "
            "is the route every one of them takes."
        ),
        "stage_code": "role-admin-approval",
        "stage_label": "Role administrator approval",
        "role_key": "school_admin",
        "scope": "SCHOOL",
    },
    {
        "code": "role-change-platform",
        "name": "Platform role permission change",
        "description": (
            "Approval for adding or removing a platform role's permissions."
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
        # A platform install that has not created its own tenant yet cannot own
        # the platform template. Skipping is right: no platform role change can
        # be raised before the tenant exists either.
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
                # The key rather than the FK: a tenant-less template names the
                # same authority in every tenant that uses it, and the FK could
                # only ever point at one tenant's copy of the role.
                "approver_role_key": spec["role_key"],
                "approver_scope": spec["scope"],
                "advance_rule": "ANY",
                "quorum_count": 0,
                "on_rejection": "TERMINAL",
                "skip_if_no_approvers": False,
            },
        )


def remove_the_ladders(apps, schema_editor):
    """Leave the central templates in place when this migration is unapplied.

    Deleting them goes through the ORM's cascade collector - template to stages
    to the stages' own routes and conditions - and in a backward run that
    unapplies several migrations at once, Django hands that collector related
    fields rendered from a different migration state than the rows it collected.
    It refuses with "Cannot query WorkflowStage object: Must be WorkflowStage
    instance", and every test that replays the graph backwards past this point
    fails on it. Ordering raw deletes by hand would restate the workflow cascade
    graph inside a migration, where it goes stale the first time a model gains a
    relation.

    Staying is safe. ``add_the_ladders`` is get_or_create for both rows, so
    reapplying finds them and writes nothing, and a template that no earlier
    migration routes anything through is inert.

    Deleting is not the harmless undo it can look like either.
    ``WorkflowInstance`` holds its template, and ``WorkflowStageInstance`` its
    stage, with PROTECT, so on any database where a role change has been
    decided, a delete refuses rather than leaving that history intact.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0014_a_reversed_vote_is_not_a_live_one"),
    ]

    operations = [
        migrations.RunPython(add_the_ladders, remove_the_ladders),
    ]
