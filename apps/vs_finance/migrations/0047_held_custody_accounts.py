"""Settlement bank codes, the held-funds roles, and a chargebacks account for existing books.

``settlement_bank_code`` lets a held tenant's settlement be paid into a branch's
collection account. ``CLIENT_FUNDS_HELD``, ``PROVIDER_BALANCE`` and
``CLIENT_FUNDS_OWED`` join the account roles; only the platform's books list them.
``CHARGEBACKS`` is every tenant's: books whose chart was seeded (they hold the
``5000`` expenses header) are given 5520 Payment Chargebacks, the account new books
are seeded with, or the first free code from 5521 to 5599 mapped to the role where
5520 is taken. The reverse removes the mappings this made and the accounts nothing
has posted to.
"""

from django.db import migrations, models

_CODE = "5520"
_NAME = "Payment Chargebacks"
_KEY = "CHARGEBACKS"


def add_chargebacks_account(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")

    headers = Account.objects.filter(code="5000").values_list("entity_id", "pk")
    for entity_id, header_id in headers.iterator():
        if FinanceAccountMapping.objects.filter(entity_id=entity_id, key=_KEY).exists():
            continue
        if Account.objects.filter(entity_id=entity_id, code=_CODE, name=_NAME).exists():
            continue
        taken = set(Account.objects.filter(entity_id=entity_id, code__startswith="55")
                    .values_list("code", flat=True))
        code = _CODE if _CODE not in taken else next(
            (str(n) for n in range(5521, 5600) if str(n) not in taken), None)
        if code is None:
            continue
        account = Account.objects.create(
            entity_id=entity_id, code=code, name=_NAME, account_type="EXPENSE",
            is_postable=True, is_contra=False, is_active=True, normal_balance="DEBIT",
            ifrs_line="ADMIN_EXPENSES", parent_id=header_id,
        )
        if code != _CODE:
            FinanceAccountMapping.objects.create(entity_id=entity_id, key=_KEY, account=account)


def remove_chargebacks_account(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")
    JournalLine = apps.get_model("vs_finance", "JournalLine")

    FinanceAccountMapping.objects.filter(key=_KEY, account__name=_NAME).delete()
    used = set(JournalLine.objects.values_list("account_id", flat=True).distinct())
    Account.objects.filter(name=_NAME, code__startswith="55").exclude(pk__in=used).exclude(
        finance_mapping_roles__isnull=False).delete()



class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0046_gateway_clearing_accounts"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankaccount",
            name="settlement_bank_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The bank's code at the payment provider, which a transfer into this account names (for example 058).",
                max_length=10,
            ),
        ),
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
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(add_chargebacks_account, remove_chargebacks_account),
    ]
