"""Register the VENDOR_ADVANCE account role so an entity can override 1240.

Choices-only change: the new role resolves to the seeded 1240 Vendor Advances
asset by default, and this lets an entity point it at its own account like every
other control role.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0019_allocation_effective_date"),
    ]

    operations = [
        migrations.AlterField(
            model_name="financeaccountmapping",
            name="key",
            field=models.CharField(
                choices=[
                    ("CASH_BANK", "Cash and bank"),
                    ("ACCOUNTS_RECEIVABLE", "Accounts receivable"),
                    ("ACCOUNTS_PAYABLE", "Accounts payable"),
                    ("CUSTOMER_CREDIT", "Customer credit"),
                    ("VENDOR_ADVANCE", "Vendor advances"),
                    ("GRIR_CLEARING", "GR/IR clearing"),
                    ("OUTPUT_VAT", "Output VAT"),
                    ("WHT_PAYABLE", "WHT payable"),
                    ("RETAINED_EARNINGS", "Retained earnings"),
                    ("BAD_DEBT_EXPENSE", "Bad debt expense"),
                    ("BANK_CHARGES", "Bank charges"),
                    ("INVENTORY_ASSET", "Inventory asset"),
                    ("INVENTORY_ADJUSTMENT", "Inventory adjustment"),
                    ("PURCHASE_PRICE_VARIANCE", "Purchase price variance"),
                ],
                max_length=32,
            ),
        ),
    ]
