from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0018_alter_invoice_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="dimension",
            name="allowed_values",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Permitted values for this axis, e.g. ['GRANT-A', 'INTERNAL']. "
                    "Empty means no values are defined yet (lines may not use the axis)."
                ),
            ),
        ),
    ]
