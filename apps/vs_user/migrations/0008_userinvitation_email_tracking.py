from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_user", "0007_alter_authattempt_options_alter_autheventlog_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userinvitation",
            name="email_status",
            field=models.CharField(
                choices=[("PENDING", "Pending"), ("SENT", "Sent"), ("FAILED", "Failed")],
                default="PENDING",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="userinvitation",
            name="email_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userinvitation",
            name="email_last_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="userinvitation",
            name="email_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
