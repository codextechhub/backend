from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0010_rename_school_role_models"),
    ]

    operations = [
        migrations.RenameField(
            model_name="prebuiltrolepermission",
            old_name="suggested_role",
            new_name="prebuilt_role",
        ),
        migrations.RenameField(
            model_name="schoolroletemplate",
            old_name="suggested_from",
            new_name="prebuilt_from",
        ),
    ]
