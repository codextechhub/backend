"""Let an inter-branch transfer carry a difference agreed when a shared bank account is split."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0061_branch_fiscal_period"),
    ]

    operations = [
        migrations.AlterField(
            model_name="interbranchtransfer",
            name="kind",
            field=models.CharField(
                choices=[
                    ("CASH", "Cash"),
                    ("FORWARDED_RECEIPT", "Forwarded receipt"),
                    ("RECEIVABLE", "Receivable"),
                    ("RECHARGE", "Recharge"),
                    ("GOODS", "Goods"),
                    ("INCOME_GIVEN_BACK", "Income given back"),
                    ("BANK_SPLIT", "Shared bank split"),
                ],
                max_length=20,
            ),
        ),
    ]
