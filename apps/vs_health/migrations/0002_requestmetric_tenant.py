"""Add the tenant dimension to RequestMetric (nullable, additive).

First step of the school -> tenant cutover for the observability rollup. The FK
is nullable: platform-anonymous / unauthenticated traffic carries no asserted
tenant. Backfill and the school drop follow in 0003/0004.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_health", "0001_initial"),
        ("vs_tenants", "0004_backfill_business_tenants"),
    ]

    operations = [
        migrations.AddField(
            model_name="requestmetric",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="vs_tenants.tenant",
            ),
        ),
    ]
