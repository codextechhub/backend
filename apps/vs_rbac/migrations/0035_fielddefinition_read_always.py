from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0034_split_permissions_name_their_own_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="fielddefinition",
            name="read_always",
            field=models.BooleanField(default=False),
        ),
    ]
