"""The cash over and short account for existing books.

New books are seeded with 5530 Cash Over and Short, where a petty cash count that
differs from the fund's books is posted. Books whose chart was seeded (they hold
the 5000 header) are given the same account here. Where 5530 is taken by another
account, the first free code in 5531-5549 is used and mapped to the role, so the
petty cash services still find it. Books that already map the role are left alone.

The reverse removes the mapping this made and the account when nothing has posted
to it.
"""

from django.db import migrations

_KEY = "CASH_OVER_SHORT"
_CODE = "5530"
_NAME = "Cash Over and Short"
_FREE = range(5531, 5550)


def add_cash_over_short_account(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")

    headers = Account.objects.filter(code="5000").values_list("entity_id", "pk")
    for entity_id, header_id in headers.iterator():
        if FinanceAccountMapping.objects.filter(entity_id=entity_id, key=_KEY).exists():
            continue
        if Account.objects.filter(entity_id=entity_id, code=_CODE, name=_NAME).exists():
            continue
        taken = set(
            Account.objects.filter(entity_id=entity_id, code__startswith="55")
            .values_list("code", flat=True)
        )
        chosen = _CODE if _CODE not in taken else next(
            (str(n) for n in _FREE if str(n) not in taken), None)
        if chosen is None:
            continue
        account = Account.objects.create(
            entity_id=entity_id, code=chosen, name=_NAME, account_type="EXPENSE",
            is_postable=True, is_contra=False, is_active=True, normal_balance="DEBIT",
            ifrs_line="ADMIN_EXPENSES", parent_id=header_id,
        )
        if chosen != _CODE:
            FinanceAccountMapping.objects.create(entity_id=entity_id, key=_KEY, account=account)


def remove_cash_over_short_account(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")
    JournalLine = apps.get_model("vs_finance", "JournalLine")

    used = set(JournalLine.objects.values_list("account_id", flat=True).distinct())
    FinanceAccountMapping.objects.filter(key=_KEY, account__name=_NAME).exclude(
        account_id__in=used).delete()
    Account.objects.filter(name=_NAME, code__startswith="55").exclude(pk__in=used).exclude(
        finance_mapping_roles__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0056_petty_cash_returns"),
    ]

    operations = [
        migrations.RunPython(add_cash_over_short_account, remove_cash_over_short_account),
    ]
