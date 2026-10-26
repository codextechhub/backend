from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_user", "0018_account_closure"),
    ]

    operations = [
        migrations.AddField(
            model_name="loginsession",
            name="previous_refresh_jti",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="loginsession",
            name="refreshed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
