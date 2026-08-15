"""Point this app's branch foreign keys at vs_tenants.Branch. State only.

Phase D of docs/architecture/school-decoupling-scope.md.

``Branch`` kept its class name, its integer primary key and its table
(``vs_schools_branch``), so the 19 ``branch_id`` columns here already
reference exactly the right rows. Only Django's idea of which model owns
them changes, and ``sqlmigrate`` on this migration prints nothing but
BEGIN/COMMIT.

The table name is what makes that true, not the wrapper. Django's
``BaseDatabaseSchemaEditor._field_should_be_altered`` ignores a changed
foreign key target when the old and new models share a ``db_table``, so these
would emit no SQL even unwrapped. ``SeparateDatabaseAndState`` is here to say
so out loud and to keep the whole phase one shape: it is *not* optional in
``vs_tenants.0004`` and ``vs_schools.0005``, where the same rename would
otherwise ``CREATE TABLE`` and ``DROP TABLE vs_schools_branch``.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0021_adjustment_threshold_on_first_stage"),
        ("vs_tenants", "0004_move_branch_from_vs_schools"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="bankaccount",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_bank_accounts",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="concession",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="creditnote",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="customer",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_customers",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="documentsequence",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_doc_sequences",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="dunningnotice",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="expenseclaim",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="feestructure",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_fee_structures",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="fixedasset",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="invoice",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="journalentry",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="payment",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="paymentplan",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="payrollrun",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="pettycashfund",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_petty_cash_funds",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="pettycashvoucher",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="refund",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="taxfiling",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
                migrations.AlterField(
                    model_name="writeoffrequest",
                    name="branch",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_set",
                        to="vs_tenants.branch",
                    ),
                ),
            ],
        ),
        # Not part of the move, and not state-only in principle - a help_text
        # edit simply produces no SQL. The example it used to give named a
        # school model, which is the one word of product vocabulary left in
        # this app's loose-reference contract.
        migrations.AlterField(
            model_name="customer",
            name="source_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Loose reference to the originating domain record's model, as 'app_label.Model'.",
                max_length=64,
            ),
        ),
    ]
