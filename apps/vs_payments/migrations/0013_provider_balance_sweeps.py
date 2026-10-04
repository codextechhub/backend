"""Count the provider's automatic settlements of the platform balance in the daily check.

Adds :class:`~vs_payments.models.ProviderSweep` (one row per settlement, unique on
provider and settlement id) and the sweep figures on each held-ledger check. Every
added column has a default that reproduces the check as it was, so existing rows
read unchanged. Reversible.
"""

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0012_payment_events_append_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="heldreconciliation",
            name="balance_swept",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="heldreconciliation",
            name="own_swept_settled",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="heldreconciliation",
            name="swept_total",
            field=models.BigIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="ProviderSweep",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "provider",
                    models.CharField(
                        choices=[("PAYSTACK", "Paystack"), ("FAKE", "Fake (testing)")],
                        max_length=16,
                    ),
                ),
                ("settlement_id", models.CharField(max_length=64)),
                ("currency", models.CharField(default="NGN", max_length=3)),
                ("amount", models.BigIntegerField()),
                ("settled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "provider_status",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                ("recorded_on", models.DateField()),
                ("raw", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "ordering": ["-settled_at", "-id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("provider", "settlement_id"),
                        name="uniq_payments_provider_sweep",
                    )
                ],
            },
        ),
    ]
