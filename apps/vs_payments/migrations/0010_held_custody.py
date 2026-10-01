"""Held custody: the platform's held-funds sub-ledger, settlement runs and switching.

Adds what the platform holds for each held-mode branch (``HeldBalance``) and every
movement of it (``HeldMovement``, with the balance after it and a chargeback's
entry in the tenant's books), the settlement run's per-branch record
(``HeldSettlement``, with the transfer fee the branch bears), the purpose of a
payout batch, the subaccount a virtual account settles to and the account that
replaced a retired one, and the note a waiting custody switch carries. Existing
rows take the defaults: every batch pays suppliers, and no collection confirmed
before this migration is held, so the held balances start at zero (a platform
operator records each branch's opening balance once with
``record_held_opening_balance``). Schema only, reversible.
"""

import django.db.models.deletion
import django.utils.timezone
import vs_finance.money
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0047_held_custody_accounts"),
        ("vs_payments", "0009_custody_and_branches"),
        ("vs_tenants", "0011_platform_tenant_lagos_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="collectionintent",
            name="held_by_platform",
            field=models.BooleanField(
                default=False,
                help_text="The money settled to the platform's provider balance, which owes it to the branch.",
            ),
        ),
        migrations.AddField(
            model_name="paymentcustodysettings",
            name="pending_note",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Why a pending change whose month has come has not taken effect yet.",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="payoutbatch",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("VENDOR", "Supplier payments"),
                    ("SETTLEMENT", "Settlement to a branch's bank"),
                ],
                default="VENDOR",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="virtualaccount",
            name="replaced_by",
            field=models.ForeignKey(
                blank=True,
                help_text="The account issued in place of this retired one.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replaces",
                to="vs_payments.virtualaccount",
            ),
        ),
        migrations.AddField(
            model_name="virtualaccount",
            name="settlement_subaccount",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The provider subaccount deposits settle to; blank when they settle to the platform's balance.",
                max_length=64,
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
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="virtualaccount",
            name="status",
            field=models.CharField(
                choices=[
                    ("ACTIVE", "Active"),
                    ("INACTIVE", "Inactive"),
                    ("RETIRED", "Retired"),
                ],
                default="ACTIVE",
                max_length=12,
            ),
        ),
        migrations.CreateModel(
            name="HeldSettlement",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Awaiting approval or transfer"),
                            ("PAID", "Paid into the branch's bank"),
                            ("FAILED", "Transfer failed; its payments are released"),
                        ],
                        default="PENDING",
                        max_length=10,
                    ),
                ),
                (
                    "run_on",
                    models.DateField(
                        help_text="The tenant's day the run built this settlement."
                    ),
                ),
                (
                    "final",
                    models.BooleanField(
                        default=False,
                        help_text="Built by a switch to direct custody, to pay out everything held.",
                    ),
                ),
                (
                    "cutoff",
                    models.DateTimeField(
                        help_text="Payments confirmed before this instant are included."
                    ),
                ),
                (
                    "gross",
                    vs_finance.money.MoneyField(
                        help_text="The claimed payments, in kobo."
                    ),
                ),
                (
                    "fees",
                    vs_finance.money.MoneyField(
                        help_text="What the provider kept from them, in kobo."
                    ),
                ),
                (
                    "amount",
                    vs_finance.money.MoneyField(
                        help_text="Kobo transferred to the branch's bank."
                    ),
                ),
                (
                    "transfer_fee",
                    vs_finance.money.MoneyField(
                        help_text="The provider's fee for the transfer, borne by the branch, in kobo."
                    ),
                ),
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                (
                    "bank_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_settlements",
                        to="vs_finance.bankaccount",
                    ),
                ),
                (
                    "batch",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_settlement",
                        to="vs_payments.payoutbatch",
                    ),
                ),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_settlements",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_settlements",
                        to="vs_finance.ledgerentity",
                    ),
                ),
                (
                    "settlement_journal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_settlements",
                        to="vs_finance.journalentry",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_settlements",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-id"],
            },
        ),
        migrations.AddField(
            model_name="collectionintent",
            name="held_settlement",
            field=models.ForeignKey(
                blank=True,
                help_text="The settlement run paying this held payment on to its branch's bank.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="collections",
                to="vs_payments.heldsettlement",
            ),
        ),
        migrations.CreateModel(
            name="HeldBalance",
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
                    "balance",
                    models.BigIntegerField(
                        default=0, help_text="Kobo held for the branch."
                    ),
                ),
                (
                    "branch",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_balance",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_balances",
                        to="vs_tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["tenant_id", "branch_id"],
                "indexes": [
                    models.Index(
                        fields=["tenant"], name="vs_payments_tenant__25d31f_idx"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="HeldMovement",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("COLLECTION", "Online payment received"),
                            ("PAYOUT", "Online payout sent"),
                            ("SETTLEMENT", "Settlement to the branch's bank"),
                            ("RELEASE", "Payout or settlement failed"),
                            ("OPENING", "Opening balance"),
                            ("DISPUTE", "Chargeback"),
                        ],
                        max_length=12,
                    ),
                ),
                (
                    "amount",
                    models.BigIntegerField(
                        help_text="Signed kobo: positive raises the held balance."
                    ),
                ),
                (
                    "balance_after",
                    models.BigIntegerField(
                        default=0, help_text="The branch's held balance after it."
                    ),
                ),
                ("occurred_on", models.DateField()),
                ("reference", models.CharField(blank=True, default="", max_length=64)),
                ("narration", models.CharField(blank=True, default="", max_length=255)),
                (
                    "journal_error",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_movements",
                        to="vs_tenants.branch",
                    ),
                ),
                (
                    "collection",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_movements",
                        to="vs_payments.collectionintent",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "payout",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_movements",
                        to="vs_payments.payoutinstruction",
                    ),
                ),
                (
                    "platform_journal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_movements",
                        to="vs_finance.journalentry",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_movements",
                        to="vs_tenants.tenant",
                    ),
                ),
                (
                    "tenant_journal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="held_tenant_movements",
                        to="vs_finance.journalentry",
                    ),
                ),
            ],
            options={
                "ordering": ["-id"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "branch"],
                        name="vs_payments_tenant__81d551_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("kind", "COLLECTION")),
                        fields=("collection",),
                        name="uniq_payments_held_collection_once",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("kind", "DISPUTE")),
                        fields=("collection",),
                        name="uniq_payments_held_dispute_once",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("kind", "OPENING")),
                        fields=("branch",),
                        name="uniq_payments_held_opening_once",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("payout__isnull", False)),
                        fields=("payout", "kind"),
                        name="uniq_payments_held_payout_kind_once",
                    ),
                ],
            },
        ),
        migrations.AddIndex(
            model_name="heldsettlement",
            index=models.Index(
                fields=["entity", "branch", "status"],
                name="vs_payments_entity__dc5888_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="heldsettlement",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "PENDING")),
                fields=("branch",),
                name="uniq_payments_held_settlement_one_pending",
            ),
        ),
        migrations.AddConstraint(
            model_name="heldsettlement",
            constraint=models.UniqueConstraint(
                fields=("branch", "run_on", "final"),
                name="uniq_payments_held_settlement_branch_day",
            ),
        ),
    ]
