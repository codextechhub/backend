from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0002_rename_vs_workflow_school__4ba373_idx_vs_workflow_school__54835e_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowinstance",
            name="document_summary",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
