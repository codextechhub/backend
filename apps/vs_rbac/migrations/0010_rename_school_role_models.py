from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0009_rename_role_templates"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="RolePermission",
            new_name="SchoolRolePermission",
        ),
        migrations.RenameModel(
            old_name="RoleGroup",
            new_name="SchoolRoleGroup",
        ),
        migrations.RenameModel(
            old_name="UserRoleAssignment",
            new_name="SchoolUserRoleAssignment",
        ),
        migrations.RenameModel(
            old_name="RoleChangeRequest",
            new_name="SchoolRoleChangeRequest",
        ),
        migrations.RenameModel(
            old_name="RoleChangeDeltaItem",
            new_name="SchoolRoleChangeDeltaItem",
        ),
    ]
