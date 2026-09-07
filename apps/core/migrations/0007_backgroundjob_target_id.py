from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_backfill_storedfile_bindings"),
    ]

    operations = [
        migrations.AddField(
            model_name="backgroundjob",
            name="target_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Primary key of the record this job is about, as text. The "
                    "label names the work in prose and cannot be followed; this "
                    "is what lets a completion notification link back to the "
                    "thing it finished."
                ),
                max_length=64,
            ),
        ),
    ]
