from django.db import migrations, models


def seed_registry_revision(apps, schema_editor):
    PermissionRegistryRevision = apps.get_model(
        "vs_rbac", "PermissionRegistryRevision",
    )
    PermissionRegistryRevision.objects.get_or_create(pk=1, defaults={"revision": 1})


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0009_remove_restricted_grant_bypasses"),
    ]

    operations = [
        migrations.CreateModel(
            name="PermissionRegistryRevision",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False,
                    ),
                ),
                ("revision", models.PositiveBigIntegerField(default=1)),
            ],
        ),
        migrations.RunPython(seed_registry_revision, migrations.RunPython.noop),
    ]
