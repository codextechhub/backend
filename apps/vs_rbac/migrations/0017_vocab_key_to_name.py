"""Merge the redundant key+name pair on the three vocab models into a single
`name` SlugField that doubles as the primary key and permission-key component.

State operations: remove the old display `name` CharField, rename `key` → `name`.
Database operations: same column renames in SQL (CHANGE key name ...).
The primary key and any index references are preserved automatically by
MariaDB when a column is renamed via CHANGE.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0016_reconcile_permission_fk_fields"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                # PermissionModule
                migrations.RemoveField(model_name="permissionmodule", name="name"),
                migrations.RenameField(model_name="permissionmodule", old_name="key", new_name="name"),
                migrations.AlterModelOptions(
                    name="permissionmodule",
                    options={"ordering": ["name"]},
                ),

                # PermissionAction
                migrations.RemoveField(model_name="permissionaction", name="name"),
                migrations.RenameField(model_name="permissionaction", old_name="key", new_name="name"),
                migrations.AlterModelOptions(
                    name="permissionaction",
                    options={"ordering": ["name"]},
                ),

                # PermissionResource
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
            ],
            database_operations=[
                # PermissionModule: drop display name column, rename key → name (PK preserved)
                migrations.RunSQL(
                    "ALTER TABLE vs_rbac_permissionmodule DROP COLUMN `name`, CHANGE `key` `name` VARCHAR(64) NOT NULL;",
                    reverse_sql=(
                        "ALTER TABLE vs_rbac_permissionmodule "
                        "CHANGE `name` `key` VARCHAR(64) NOT NULL, "
                        "ADD COLUMN `name` VARCHAR(120) NOT NULL DEFAULT '';"
                    ),
                ),
                # PermissionAction: same
                migrations.RunSQL(
                    "ALTER TABLE vs_rbac_permissionaction DROP COLUMN `name`, CHANGE `key` `name` VARCHAR(64) NOT NULL;",
                    reverse_sql=(
                        "ALTER TABLE vs_rbac_permissionaction "
                        "CHANGE `name` `key` VARCHAR(64) NOT NULL, "
                        "ADD COLUMN `name` VARCHAR(120) NOT NULL DEFAULT '';"
                    ),
                ),
                # PermissionResource: same (unique index on module_id+key auto-updates to module_id+name)
                migrations.RunSQL(
                    "ALTER TABLE vs_rbac_permissionresource DROP COLUMN `name`, CHANGE `key` `name` VARCHAR(64) NOT NULL;",
                    reverse_sql=(
                        "ALTER TABLE vs_rbac_permissionresource "
                        "CHANGE `name` `key` VARCHAR(64) NOT NULL, "
                        "ADD COLUMN `name` VARCHAR(120) NOT NULL DEFAULT '';"
                    ),
                ),
            ],
        ),
    ]
