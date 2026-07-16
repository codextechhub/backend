from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_admin_console", "0002_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="impersonationsession",
            name="ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
