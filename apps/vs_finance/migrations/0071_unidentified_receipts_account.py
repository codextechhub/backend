"""The unidentified receipts account for existing books.

New books are seeded with 2195 Unidentified Receipts, where an online payment a
branch's payment terminal received waits until a person matches it to the
customer who paid. Books whose chart was seeded (they hold the 2000 liabilities
header) are given the same account here. Where 2195 is taken by another account,
the first free code from 2196 to 2199 is used and mapped to the role, so the
services still find it. Books that already map the role are left alone.

The reverse removes the mappings this made and the accounts nothing has posted to.
"""

from django.db import migrations, models


_KEY = "UNIDENTIFIED_RECEIPTS"
_CODE = "2195"
_NAME = "Unidentified Receipts"


def add_unidentified_receipts_account(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")

    headers = Account.objects.filter(code="2000").values_list("entity_id", "pk")
    for entity_id, header_id in headers.iterator():
        if FinanceAccountMapping.objects.filter(entity_id=entity_id, key=_KEY).exists():
            continue
        if Account.objects.filter(entity_id=entity_id, code=_CODE, name=_NAME).exists():
            continue
        taken = set(Account.objects.filter(entity_id=entity_id, code__startswith="21")
                    .values_list("code", flat=True))
        chosen = _CODE if _CODE not in taken else next(
            (str(n) for n in range(2196, 2200) if str(n) not in taken), None)
        if chosen is None:
            continue
        account = Account.objects.create(
            entity_id=entity_id, code=chosen, name=_NAME, account_type="LIABILITY",
            is_postable=True, is_contra=False, is_active=True, normal_balance="CREDIT",
            ifrs_line="TRADE_PAYABLES", parent_id=header_id,
        )
        if chosen != _CODE:
            FinanceAccountMapping.objects.create(entity_id=entity_id, key=_KEY, account=account)


def remove_unidentified_receipts_account(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")
    JournalLine = apps.get_model("vs_finance", "JournalLine")

    used = set(JournalLine.objects.values_list("account_id", flat=True).distinct())
    FinanceAccountMapping.objects.filter(key=_KEY, account__name=_NAME).exclude(
        account_id__in=used).delete()
    Account.objects.filter(name=_NAME, code__startswith="219").exclude(pk__in=used).exclude(
        finance_mapping_roles__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0070_simple_account_names"),
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
                    ("UNIDENTIFIED_RECEIPTS", "Unidentified receipts"),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(add_unidentified_receipts_account, remove_unidentified_receipts_account),
    ]
