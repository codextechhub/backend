"""Give every branch's own role the name that branch has now.

Each branch carries its own copy of Branch Admin, and that copy's name is
composed from the branch's name. The composition happened once, at
provisioning, and nothing re-composed it, so a school that renamed Ikeja to
Yaba kept "Branch Admin - Ikeja" on its roles screen with no way to correct it.
The name follows the branch from now on (``vs_rbac.signals.rename_branch_roles``);
these are the rows that went stale before it did.

The three rules the live sync keeps apply here, for the same reasons. A school
that gave the role its own name keeps that name, because renaming a branch is
not permission to rename what a school decided to call something. A school with
one branch loses a suffix that only repeats what the row above it already says.
And a name another role in the tenant already holds is left alone rather than
failing the whole migration on a unique constraint.

Reversible as a no-op. What it writes are the names the product composes today,
so going back would only restore names that disagree with their branch.
"""
from django.db import migrations


def follow_the_branch(apps, schema_editor):
    Branch = apps.get_model("vs_tenants", "Branch")
    Prebuilt = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")

    library = dict(Prebuilt.objects.values_list("key", "name"))
    if not library:
        return

    branch_counts = {}
    for role in Role.objects.filter(
        branch__isnull=False, is_system_role=True,
    ).select_related("branch"):
        branch = role.branch
        suffix = f"-{branch.pk}"
        if not role.key.endswith(suffix):
            continue
        library_name = library.get(role.key[: -len(suffix)])
        if library_name is None:
            continue
        # Only a name composed from the library's is rewritten; anything else
        # is the school's own wording.
        if role.name != library_name and not role.name.startswith(f"{library_name} - "):
            continue

        if role.tenant_id not in branch_counts:
            branch_counts[role.tenant_id] = Branch.objects.filter(
                tenant_id=role.tenant_id,
            ).count()
        siblings = branch_counts[role.tenant_id]
        wanted = library_name if siblings <= 1 else f"{library_name} - {branch.name}"
        if wanted == role.name:
            continue
        if Role.objects.filter(
            tenant_id=role.tenant_id, name=wanted,
        ).exclude(pk=role.pk).exists():
            continue

        role.name = wanted
        role.save(update_fields=["name"])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0019_ticket_assignment_is_the_desks_own"),
        ("vs_tenants", "0010_remove_branch__type"),
    ]
    operations = [
        migrations.RunPython(follow_the_branch, migrations.RunPython.noop),
    ]
