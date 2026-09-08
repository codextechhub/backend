"""Rename the branches a console default called "Main Campus".

A school site is a branch. That is the word the model uses, the word the API
returns and the word the product says on screen, and "campus" is not a synonym
we accept anywhere.

The console's create-school form filled the first branch's name in for the
operator and filled in "Main Campus", so four schools were created carrying a
word the rest of the product does not use. The form is fixed; this is the rows
it already wrote.

Role names go with them. ``provision_role_from_prebuilt`` composes a
branch-scoped role's name as ``"{prebuilt name} - {branch name}"`` and stores the
result, so renaming the branch alone would leave "Branch Admin - Main Campus"
behind, which is where the word was actually being read.

Only the exact string is touched. A school that deliberately calls a site
"Riverside Campus" is using its own name for its own place, and that is theirs
to choose; this corrects a default nobody chose.

Not reversible. Going back would rename every "Main Branch" to "Main Campus",
and after this the form creates them with that name deliberately - so the
reverse would rename rows that were always right, to a word the product does not
use. Nothing is lost by staying: a school may rename its own branch whenever it
likes.
"""
from django.db import migrations


def say_branch(apps, schema_editor):
    Branch = apps.get_model("vs_tenants", "Branch")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")

    for branch in Branch.objects.filter(name="Main Campus"):
        branch.name = "Main Branch"
        branch.save(update_fields=["name"])
        # The role's name was snapshotted from the branch's, so it does not
        # follow on its own.
        Role.objects.filter(
            tenant=branch.tenant, branch=branch, name="Branch Admin - Main Campus",
        ).update(name="Branch Admin - Main Branch")

    # Any that were renamed by hand, or whose role was created before the
    # branch's own rename, are caught by the name alone.
    Role.objects.filter(name="Branch Admin - Main Campus").update(
        name="Branch Admin - Main Branch",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0008_alter_branch_name"),
        ("vs_rbac", "0014_a_school_owns_the_roles_and_rules_codex_set_up"),
    ]
    operations = [migrations.RunPython(say_branch, migrations.RunPython.noop)]
