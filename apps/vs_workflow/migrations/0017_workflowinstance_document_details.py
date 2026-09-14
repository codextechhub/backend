from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_workflow", "0016_named_dynamic_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowinstance",
            name="document_details",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
