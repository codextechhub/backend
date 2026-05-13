"""Make Permission.module, resource, action non-nullable.

db_constraint=False is kept so MariaDB does not enforce a DB-level FK constraint
(MariaDB has known issues with varchar-typed FK targets and mixed charsets).
Django's on_delete and serializer-level validation still fully enforce integrity.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0014_populate_permission_vocab"),
    ]

    operations = [
        migrations.AlterField(
            model_name="permission",
            name="module",
            field=models.ForeignKey(
                db_column="module_key",
                db_constraint=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="permissions",
                to="vs_rbac.permissionmodule",
                to_field="key",
            ),
        ),
        migrations.AlterField(
            model_name="permission",
            name="resource",
            field=models.ForeignKey(
                db_constraint=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="permissions",
                to="vs_rbac.permissionresource",
            ),
        ),
        migrations.AlterField(
            model_name="permission",
            name="action",
            field=models.ForeignKey(
                db_column="action_key",
                db_constraint=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="permissions",
                to="vs_rbac.permissionaction",
                to_field="key",
            ),
        ),
    ]
