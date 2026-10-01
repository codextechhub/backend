"""Gateway records carry a branch, collections track clearing and settlement, and custody is a setting.

Every column added is nullable or defaulted, so existing rows are untouched: their
branch is filled by the branch backfill from the facts already on them, and a
collection confirmed before clearing existed keeps no clearing account and so never
waits for a settlement match. A tenant with no custody row is held, as before.
"""

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import vs_finance.money
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0046_gateway_clearing_accounts"),
        ("vs_payments", "0008_who_really_acted_under_a_proxy"),
        ("vs_tenants", "0010_remove_branch__type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentCustodySettings",
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
                    "mode",
                    models.CharField(
                        choices=[
                            ("DIRECT", "Direct to each branch's bank"),
                            ("HELD", "Held by the platform"),
                        ],
                        default="HELD",
                        max_length=8,
                    ),
                ),
                ("effective_from", models.DateField(blank=True, null=True)),
                (
                    "pending_mode",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("DIRECT", "Direct to each branch's bank"),
                            ("HELD", "Held by the platform"),
                        ],
                        default="",
                        max_length=8,
                    ),
                ),
                ("pending_from", models.DateField(blank=True, null=True)),
                (
                    "settlement_interval_days",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(7),
                        ],
                    ),
                ),
                (
                    "clearing_stale_days",
                    models.PositiveSmallIntegerField(
                        default=7,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(60),
                        ],
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "payment custody settings",
            },
        ),
        migrations.AddField(
            model_name="collectionintent",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch the money belongs to (vs_payments.services.collection_branch_id).",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payment_collections",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="collectionintent",
            name="clearing_account",
            field=models.ForeignKey(
                blank=True,
                help_text="The gateway clearing account the receipt debited, until settlement.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="clearing_collections",
                to="vs_finance.account",
            ),
        ),
        migrations.AddField(
            model_name="collectionintent",
            name="fee",
            field=vs_finance.money.MoneyField(
                blank=True,
                default=None,
                help_text="Kobo the provider kept from this payment, as it reported on confirmation.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="collectionintent",
            name="settlement_entry",
            field=models.ForeignKey(
                blank=True,
                help_text="The journal that moved this payment from clearing to a bank.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="settled_collections",
                to="vs_finance.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="payoutbatch",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The one branch every line of the batch pays from.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payout_batches",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="payoutinstruction",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch whose bank account the money leaves (its source account's).",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payout_instructions",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="virtualaccount",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch whose money arrives here: its customer's, else its deposit bank's.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payment_virtual_accounts",
                to="vs_tenants.branch",
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
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="collectionintent",
            index=models.Index(
                fields=["entity", "branch", "status"],
                name="vs_payments_entity__3de429_idx",
            ),
        ),
        migrations.AddField(
            model_name="paymentcustodysettings",
            name="tenant",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payment_custody",
                to="vs_tenants.tenant",
            ),
        ),
        migrations.AddField(
            model_name="paymentcustodysettings",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
