"""Drop the legacy school dimension from RequestMetric.

Final step of the school -> tenant cutover: swap the unique tuple and slicing
index onto ``tenant`` and remove the ``school`` FK. The table stays global
observability data (unscoped); ``tenant`` is a slicing dimension, not isolation.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_health", "0003_backfill_requestmetric_tenant"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="requestmetric",
            unique_together={("bucket_start", "route", "method", "tenant")},
        ),
        migrations.AddIndex(
            model_name="requestmetric",
            index=models.Index(
                fields=["bucket_start", "tenant"], name="vs_health_r_bucket_tenant_idx"
            ),
        ),
        migrations.RemoveIndex(
            model_name="requestmetric",
            name="vs_health_r_bucket__4d2f1d_idx",
        ),
        migrations.RemoveField(
            model_name="requestmetric",
            name="school",
        ),
    ]
