"""The seeded accountant-facing accounts carry the term with its plain words.

Account mapping roles and the starter chart name receivables, payables,
withholding tax and gateway clearing as an accountant knows them with the
bursar's words beside them: "Accounts receivable (what customers owe)",
"Accounts payable (what is owed to suppliers)", "WHT payable (withholding tax)"
and "Gateway clearing (online payments not yet in the bank)".

An existing account is renamed only where it still carries the exact name the
chart seeded. Gateway clearing may sit at any code from 1125 to 1199
(``0046_gateway_clearing_accounts`` placed it at the first free code when 1125
was taken). A school that renamed one of these accounts keeps its own name.
Reversing renames back only the accounts holding the new name.

The names are written out here rather than imported, so the migration keeps
meaning what it meant when it was written.
"""

from django.db import migrations, models

#: (codes, name the chart seeded, name it carries now).
RENAMES = (
    (("1200",), "Accounts Receivable", "Accounts receivable (what customers owe)"),
    (("2100",), "Accounts Payable", "Accounts payable (what is owed to suppliers)"),
    (("2300",), "WHT Payable", "WHT payable (withholding tax)"),
    (tuple(str(code) for code in range(1125, 1200)), "Gateway Clearing",
     "Gateway clearing (online payments not yet in the bank)"),
)


def rename_seeded(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    for codes, before, now in RENAMES:
        Account.objects.filter(code__in=codes, name=before).update(name=now)


def restore_seeded(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    for codes, before, now in RENAMES:
        Account.objects.filter(code__in=codes, name=now).update(name=before)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0067_plain_account_labels"),
    ]

    operations = [
        migrations.AlterField(
            model_name="financeaccountmapping",
            name="key",
            field=models.CharField(
                choices=[
                    ("CASH_BANK", "Cash and bank"),
                    ("ACCOUNTS_RECEIVABLE", "Accounts receivable (what customers owe)"),
                    ("ACCOUNTS_PAYABLE", "Accounts payable (what is owed to suppliers)"),
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
                    (
                        "GATEWAY_CLEARING",
                        "Gateway clearing (online payments not yet in the bank)",
                    ),
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
        migrations.RunPython(rename_seeded, restore_seeded),
    ]
