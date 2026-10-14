"""Payroll as a payout purpose, and the waiting-for-funds batch status.

Choice changes only: a ``PAYROLL`` purpose for a branch's staff pay and a
``WAITING_FOR_FUNDS`` batch status. No data changes. Reversible.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0016_terminal_destinations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payoutbatch",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("VENDOR", "Supplier payments"),
                    ("SETTLEMENT", "Settlement to a branch's bank"),
                    ("PAYROLL", "Staff pay for a payroll run"),
                ],
                default="VENDOR",
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="payoutbatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("PROCESSING", "Processing"),
                    ("COMPLETED", "Completed"),
                    ("PARTIALLY_COMPLETED", "Partially completed"),
                    ("FAILED", "Failed"),
                    ("WAITING_FOR_FUNDS", "Waiting for funds"),
                ],
                default="DRAFT",
                max_length=20,
            ),
        ),
    ]
