"""Income given back names the credit note or concession that took it.

A write-off of a moved bill is borne by the branch holding the debt and gives
nothing back, so only a credit note or concession links a transfer here. The
column is unchanged; only its description is.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0054_income_given_back"),
    ]

    operations = [
        migrations.AlterField(
            model_name="interbranchtransfer",
            name="adjustment_entry",
            field=models.ForeignKey(
                blank=True,
                help_text="For income given back: the credit note or concession journal that took it.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="income_given_back",
                to="vs_finance.journalentry",
            ),
        ),
    ]
