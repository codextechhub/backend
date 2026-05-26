from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0002_initial"),
    ]

    operations = [
        # Use SeparateDatabaseAndState so that:
        #   - The ORM state gains the version field (Django knows it exists).
        #   - The DB ALTER uses IF NOT EXISTS, safe for production where the
        #     column already exists as NOT NULL DEFAULT '1.0'.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE vs_import_data_importtemplate "
                        "ADD COLUMN IF NOT EXISTS version VARCHAR(20) NOT NULL DEFAULT '1.0';"
                    ),
                    reverse_sql=(
                        "ALTER TABLE vs_import_data_importtemplate "
                        "DROP COLUMN IF EXISTS version;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="importtemplate",
                    name="version",
                    field=models.CharField(default="1.0", max_length=20),
                ),
            ],
        ),
    ]
