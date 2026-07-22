from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0002_alter_user_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="platformstaffprofile",
            name="job_title",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
    ]
