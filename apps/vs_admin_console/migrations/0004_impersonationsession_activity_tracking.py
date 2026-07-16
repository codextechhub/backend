import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_admin_console", "0003_impersonationsession_optional_ends_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="impersonationsession",
            name="last_activity_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="impersonationsession",
            name="access_log",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
