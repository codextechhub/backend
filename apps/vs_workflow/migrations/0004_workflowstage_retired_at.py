from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0003_workflowinstance_document_summary"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowstage",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
