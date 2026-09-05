"""Finance Admin and Procurement Admin are CodeX's, and now say so.

CodeX provisions five roles for every school: School Admin, Branch Admin,
Teacher, Finance Admin and Procurement Admin. Three carried
``is_system_role``; the two the console adopts later did not, because the
command that creates them never set it.

The flag is not decoration. The roles screen splits on it - "roles CodeX set up
for you" against "roles you added" - so a school saw two of CodeX's own roles
filed as its own work, and ``vs_workflow.services.approvers`` reads the same
flag when resolving a ROLE stage's approvers, so a key that ought to be
provisioning's resolves to nobody.

Scoped to schools by key: a tenant role named Finance Admin by a school of its
own accord would be caught by a name match, and this is not that.
"""
from django.db import migrations

CODEX_KEYS = ["finance-admin", "procurement-admin"]


def flag(apps, schema_editor):
    apps.get_model("vs_rbac", "TenantRoleTemplate").objects.filter(
        key__in=CODEX_KEYS, is_system_role=False,
    ).update(is_system_role=True)


def unflag(apps, schema_editor):
    apps.get_model("vs_rbac", "TenantRoleTemplate").objects.filter(
        key__in=CODEX_KEYS, is_system_role=True,
    ).update(is_system_role=False)


class Migration(migrations.Migration):
    dependencies = [("vs_rbac", "0010_permissionregistryrevision")]
    operations = [migrations.RunPython(flag, unflag)]
