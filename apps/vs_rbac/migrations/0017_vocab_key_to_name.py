"""Merge the redundant key+name pair on the three vocab models into a single
`name` SlugField that doubles as the primary key and permission-key component.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0016_reconcile_permission_fk_fields"),
    ]

    operations = [
        # PermissionModule: drop display name, rename key → name
        migrations.RemoveField(model_name="permissionmodule", name="name"),
        migrations.RenameField(model_name="permissionmodule", old_name="key", new_name="name"),
        migrations.AlterModelOptions(
            name="permissionmodule",
            options={"ordering": ["name"]},
        ),

        # PermissionAction: same
        migrations.RemoveField(model_name="permissionaction", name="name"),
        migrations.RenameField(model_name="permissionaction", old_name="key", new_name="name"),
        migrations.AlterModelOptions(
            name="permissionaction",
            options={"ordering": ["name"]},
        ),

        # PermissionResource: same
        migrations.RemoveField(model_name="permissionresource", name="name"),
        migrations.RenameField(model_name="permissionresource", old_name="key", new_name="name"),
        migrations.AlterUniqueTogether(
            name="permissionresource",
            unique_together={("module", "name")},
        ),
        migrations.AlterModelOptions(
            name="permissionresource",
            options={"ordering": ["module", "name"]},
        ),
    ]
