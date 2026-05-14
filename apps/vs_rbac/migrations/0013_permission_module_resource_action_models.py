import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0012_alter_prebuiltrolepermission_options_and_more"),
    ]

    operations = [
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
        migrations.RemoveIndex(
            model_name="permission",
            name="vs_rbac_per_module__20e8c1_idx",
        ),
        migrations.RemoveField(
            model_name="permission",
            name="module_key",
        ),
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
    ]
