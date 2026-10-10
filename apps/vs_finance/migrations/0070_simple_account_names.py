"""Remove explanatory brackets from the seeded control-account names.

Only exact platform-supplied names are shortened. A school that renamed an
account keeps its own wording.
"""

from django.db import migrations, models


RENAMES = (
    (("1200",), "Accounts receivable (what customers owe)", "Accounts receivable"),
    (("2100",), "Accounts payable (what is owed to suppliers)", "Accounts payable"),
    (("2150",), "GR/IR clearing (goods received, not yet billed)", "GR/IR clearing"),
    (("2300",), "WHT payable (withholding tax)", "WHT payable"),
    (
        tuple(str(code) for code in range(1125, 1200)),
        "Gateway clearing (online payments not yet in the bank)",
        "Gateway clearing",
    ),
)


def shorten_seeded_names(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    for codes, before, after in RENAMES:
        Account.objects.filter(code__in=codes, name=before).update(name=after)


def restore_paired_names(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    for codes, before, after in RENAMES:
        Account.objects.filter(code__in=codes, name=after).update(name=before)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0069_paye_source_labels"),
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
        migrations.RunPython(shorten_seeded_names, restore_paired_names),
    ]
