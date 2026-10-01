"""Chargebacks won back, and the daily check of the platform's books against its provider balance.

``DISPUTE_WON`` is the held movement that gives a branch back a chargeback the
platform won, once per dispute; ``PROVIDER_DISPUTE_RESOLVED`` audits a dispute's
outcome. ``HeldReconciliation`` records each day's comparison of the provider's
balance with the platform's books. Schema only, reversible.
"""

import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0047_held_custody_accounts"),
        ("vs_payments", "0010_held_custody"),
        ("vs_tenants", "0011_platform_tenant_lagos_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="HeldReconciliation",
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
                ("currency", models.CharField(default="NGN", max_length=3)),
                ("checked_on", models.DateField()),
                ("provider_balance", models.BigIntegerField(blank=True, null=True)),
                ("provider_account", models.BigIntegerField(default=0)),
                ("held_total", models.BigIntegerField(default=0)),
                ("own_in_transit", models.BigIntegerField(default=0)),
                ("books_balance", models.BigIntegerField(default=0)),
                ("difference", models.BigIntegerField(blank=True, null=True)),
                ("tolerance", models.BigIntegerField(default=0)),
                ("agrees", models.BooleanField(default=False)),
                ("error", models.CharField(blank=True, default="", max_length=255)),
                (
                    "incident_code",
                    models.CharField(blank=True, default="", max_length=32),
                ),
            ],
            options={
                "ordering": ["-checked_on", "-id"],
            },
        ),
        migrations.AlterField(
            model_name="heldmovement",
            name="kind",
            field=models.CharField(
                choices=[
                    ("COLLECTION", "Online payment received"),
                    ("PAYOUT", "Online payout sent"),
                    ("SETTLEMENT", "Settlement to the branch's bank"),
                    ("RELEASE", "Payout or settlement failed"),
                    ("OPENING", "Opening balance"),
                    ("DISPUTE", "Chargeback"),
                    ("DISPUTE_WON", "Chargeback won back"),
                ],
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="paymentevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("COLLECTION_INITIATED", "Collection initiated"),
                    ("COLLECTION_CONFIRMED", "Collection confirmed"),
                    ("COLLECTION_FAILED", "Collection failed"),
                    ("VIRTUAL_ACCOUNT_CREATED", "Virtual account created"),
                    (
                        "VIRTUAL_ACCOUNT_STATUS_CHANGED",
                        "Virtual account status changed",
                    ),
                    ("PAYOUT_INITIATED", "Payout initiated"),
                    ("PAYOUT_CONFIRMED", "Payout confirmed"),
                    ("PAYOUT_FAILED", "Payout failed"),
                    ("PAYOUT_BATCH_CREATED", "Payout batch created"),
                    ("PAYOUT_BATCH_SUBMITTED", "Payout batch submitted"),
                    ("WEBHOOK_RECEIVED", "Webhook received"),
                    ("WEBHOOK_REJECTED", "Webhook rejected"),
                    ("COLLECTIONS_SETTLED", "Collections settled to a bank"),
                    ("SUBACCOUNT_SAVED", "Collection subaccount saved"),
                    ("CUSTODY_SETTINGS_UPDATED", "Custody settings updated"),
                    ("CUSTODY_SWITCHED", "Custody mode switched"),
                    ("CUSTODY_SWITCH_WAITING", "Custody switch waiting"),
                    ("HELD_SETTLEMENT_BUILT", "Held settlement built"),
                    ("HELD_SETTLEMENT_PAID", "Held settlement paid"),
                    ("HELD_SETTLEMENT_FAILED", "Held settlement failed"),
                    ("HELD_FUNDS_REFUSED", "Payout refused: held funds"),
                    ("HELD_OPENING_BALANCE", "Held opening balance"),
                    ("VIRTUAL_ACCOUNT_REISSUED", "Virtual account reissued"),
                    ("PROVIDER_DISPUTE_RECEIVED", "Chargeback or refund received"),
                    ("PROVIDER_DISPUTE_RESOLVED", "Chargeback resolved"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="heldmovement",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "DISPUTE_WON")),
                fields=("collection",),
                name="uniq_payments_held_dispute_won_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="heldreconciliation",
            constraint=models.UniqueConstraint(
                fields=("provider", "currency", "checked_on"),
                name="uniq_payments_held_reconciliation_day",
            ),
        ),
    ]
