from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0008_remove_roletemplate_slug_alter_roletemplate_id"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="SuggestedRoleTemplate",
            new_name="PrebuiltRoleTemplate",
        ),
        migrations.RenameModel(
            old_name="SuggestedRolePermission",
            new_name="PrebuiltRolePermission",
        ),
        migrations.RenameModel(
            old_name="RoleTemplate",
            new_name="SchoolRoleTemplate",
        ),
    ]
