# Rewritten to use SeparateDatabaseAndState because the first attempt partially
# applied to the DB before failing. The state_operations keep Django's ORM model
# state correct; the database_operations only do what the DB actually needs from
# its current partial state (action_key renamed, module_key removed, no vocab tables).

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


PERMISSIONACTION_SQL = """
CREATE TABLE `vs_rbac_permissionaction` (
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `key`        varchar(64)  NOT NULL,
  `name`       varchar(120) NOT NULL,
  `description` longtext    NOT NULL,
  `is_active`  tinyint(1)   NOT NULL,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

PERMISSIONMODULE_SQL = """
CREATE TABLE `vs_rbac_permissionmodule` (
  `created_at` datetime(6) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  `key`        varchar(64)  NOT NULL,
  `name`       varchar(120) NOT NULL,
  `description` longtext    NOT NULL,
  `is_active`  tinyint(1)   NOT NULL,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

PERMISSIONRESOURCE_SQL = """
CREATE TABLE `vs_rbac_permissionresource` (
  `id`          bigint(20)   NOT NULL AUTO_INCREMENT,
  `created_at`  datetime(6)  NOT NULL,
  `updated_at`  datetime(6)  NOT NULL,
  `key`         varchar(64)  NOT NULL,
  `name`        varchar(120) NOT NULL,
  `description` longtext     NOT NULL,
  `is_active`   tinyint(1)   NOT NULL,
  `module_id`   varchar(64)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `vs_rbac_permissionresource_module_id_key_1f2a4d82_uniq` (`module_id`, `key`),
  CONSTRAINT `vs_rbac_permissionre_module_id_fk` FOREIGN KEY (`module_id`)
    REFERENCES `vs_rbac_permissionmodule` (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0012_alter_prebuiltrolepermission_options_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # ── State operations (Django ORM model state) ──────────────────
            # These tell Django what the models look like after this migration.
            # They are NOT executed against the database.
            state_operations=[
                migrations.CreateModel(
                    name="PermissionAction",
                    fields=[
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        ("key", models.SlugField(max_length=64, primary_key=True, serialize=False)),
                        ("name", models.CharField(max_length=120)),
                        ("description", models.TextField(blank=True)),
                        ("is_active", models.BooleanField(default=True)),
                    ],
                    options={"ordering": ["key"]},
                ),
                migrations.CreateModel(
                    name="PermissionModule",
                    fields=[
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        ("key", models.SlugField(max_length=64, primary_key=True, serialize=False)),
                        ("name", models.CharField(max_length=120)),
                        ("description", models.TextField(blank=True)),
                        ("is_active", models.BooleanField(default=True)),
                    ],
                    options={"ordering": ["key"]},
                ),
                migrations.CreateModel(
                    name="PermissionResource",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        ("key", models.SlugField(max_length=64)),
                        ("name", models.CharField(max_length=120)),
                        ("description", models.TextField(blank=True)),
                        ("is_active", models.BooleanField(default=True)),
                    ],
                    options={"ordering": ["module", "key"]},
                ),
                migrations.RemoveIndex(model_name="permission", name="vs_rbac_per_module__20e8c1_idx"),
                migrations.RemoveField(model_name="permission", name="module_key"),
                migrations.AlterField(
                    model_name="permission",
                    name="action",
                    field=models.ForeignKey(
                        db_column="action_key",
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="permissions",
                        to="vs_rbac.permissionaction",
                    ),
                ),
                migrations.AddField(
                    model_name="permission",
                    name="module",
                    field=models.ForeignKey(
                        db_column="module_key",
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="permissions",
                        to="vs_rbac.permissionmodule",
                    ),
                ),
                migrations.AddField(
                    model_name="permissionresource",
                    name="module",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="resources",
                        to="vs_rbac.permissionmodule",
                    ),
                ),
                migrations.AlterField(
                    model_name="permission",
                    name="resource",
                    field=models.ForeignKey(
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="permissions",
                        to="vs_rbac.permissionresource",
                    ),
                ),
                migrations.AddIndex(
                    model_name="permission",
                    index=models.Index(fields=["module", "action"], name="vs_rbac_per_module__5ed1a7_idx"),
                ),
                migrations.AlterUniqueTogether(
                    name="permissionresource",
                    unique_together={("module", "key")},
                ),
            ],

            # ── Database operations (actual SQL against the current DB state) ─
            # The DB already has: action_key (VARCHAR), no module_key, resource (VARCHAR).
            # Vocab tables do not exist. We create them and adjust the permission table.
            database_operations=[
                migrations.RunSQL(PERMISSIONACTION_SQL, reverse_sql="DROP TABLE IF EXISTS vs_rbac_permissionaction;"),
                migrations.RunSQL(PERMISSIONMODULE_SQL, reverse_sql="DROP TABLE IF EXISTS vs_rbac_permissionmodule;"),
                migrations.RunSQL(PERMISSIONRESOURCE_SQL, reverse_sql="DROP TABLE IF EXISTS vs_rbac_permissionresource;"),

                # Add module_key column (FK to permissionmodule, no constraint yet)
                migrations.RunSQL(
                    "ALTER TABLE vs_rbac_permission ADD COLUMN module_key VARCHAR(64) NULL;",
                    reverse_sql="ALTER TABLE vs_rbac_permission DROP COLUMN module_key;",
                ),

                # Replace resource VARCHAR with resource_id FK column
                migrations.RunSQL(
                    "ALTER TABLE vs_rbac_permission ADD COLUMN resource_id BIGINT NULL;",
                    reverse_sql="ALTER TABLE vs_rbac_permission DROP COLUMN resource_id;",
                ),
                migrations.RunSQL(
                    "ALTER TABLE vs_rbac_permission DROP COLUMN resource;",
                    reverse_sql="ALTER TABLE vs_rbac_permission ADD COLUMN resource VARCHAR(64) NOT NULL DEFAULT '';",
                ),

                # Add composite index on (module_key, action_key)
                migrations.RunSQL(
                    "CREATE INDEX vs_rbac_per_module__5ed1a7_idx ON vs_rbac_permission (module_key(64), action_key(64));",
                    reverse_sql="DROP INDEX vs_rbac_per_module__5ed1a7_idx ON vs_rbac_permission;",
                ),
            ],
        ),
    ]
