"""Account roles and payroll choices in words, and the goods-received account named for both readers.

The account mapping names the goods-received clearing role "GR/IR clearing
(goods received, not yet billed)" and the withholding tax role "WHT payable
(withholding tax)"; the PAYE method that reads figures from the salary
structure says so rather than "Supplied by the tenant". Choice labels only.

Account 2150 is renamed to "GR/IR clearing (goods received, not yet billed)"
only where it still carries the name the chart seeded, "GR/IR Clearing". A
school that named it something else keeps its own name. Reversing renames
back only the accounts holding the new name.
"""

from django.db import migrations, models

SEEDED_BEFORE = "GR/IR Clearing"
SEEDED_NOW = "GR/IR clearing (goods received, not yet billed)"


def rename_seeded(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    Account.objects.filter(code="2150", name=SEEDED_BEFORE).update(name=SEEDED_NOW)


def restore_seeded(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    Account.objects.filter(code="2150", name=SEEDED_NOW).update(name=SEEDED_BEFORE)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0066_credit_note_kind_labels"),
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
                    (
                        "GRIR_CLEARING",
                        "GR/IR clearing (goods received, not yet billed)",
                    ),
                    ("OUTPUT_VAT", "Output VAT"),
                    ("WHT_PAYABLE", "WHT payable (withholding tax)"),
                    ("RETAINED_EARNINGS", "Retained earnings"),
                    ("BAD_DEBT_EXPENSE", "Bad debt expense"),
                    ("BANK_CHARGES", "Bank charges"),
                    ("INVENTORY_ASSET", "Inventory asset"),
                    ("INVENTORY_ADJUSTMENT", "Inventory adjustment"),
                    ("PURCHASE_PRICE_VARIANCE", "Purchase price variance"),
                    ("DEFERRED_INCOME", "Deferred income"),
                    ("DEPOSITS_HELD", "Customer deposits held"),
                    ("DOUBTFUL_DEBT_ALLOWANCE", "Allowance for doubtful debts"),
                    ("BAD_DEBT_RECOVERED", "Bad debts recovered"),
                    ("FORFEITED_DEPOSIT_INCOME", "Forfeited deposits income"),
                    ("GATEWAY_CLEARING", "Gateway clearing"),
                    ("CLIENT_FUNDS_HELD", "Client funds held"),
                    ("PROVIDER_BALANCE", "Payment provider balance"),
                    ("CLIENT_FUNDS_OWED", "Owed by clients"),
                    ("CHARGEBACKS", "Payment chargebacks"),
                    ("INTER_BRANCH", "Inter-branch balances"),
                    ("HELD_FOR_OTHER_BRANCHES", "Held for other branches"),
                    ("CASH_OVER_SHORT", "Cash over and short"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="financepayrollsettings",
            name="paye_method",
            field=models.CharField(
                choices=[
                    ("COMPUTED", "Computed from the national tax table"),
                    ("SUPPLIED", "Taken from the salary structure or roster"),
                ],
                default="COMPUTED",
                max_length=10,
            ),
        ),
        migrations.RunPython(rename_seeded, restore_seeded),
    ]
