"""The inter-branch balances and held-for-other-branches accounts for existing books.

New books are seeded with 1260 Inter-branch Balances and 2190 Held for Other
Branches. Books whose chart was seeded (they hold the 1000 and 2000 headers) are
given the same accounts here. Where a code is taken by another account, the first
free code in its range (1261-1299, 2191-2199) is used and mapped to the role, so
the services still find it. Books that already map a role are left alone.

The reverse removes the mappings this made and the accounts nothing has posted to.
"""

from django.db import migrations

#: (role, default code, name, type, header code, normal balance, IFRS line, free range)
_ACCOUNTS = (
    ("INTER_BRANCH", "1260", "Inter-branch Balances", "ASSET", "1000", "DEBIT",
     "OTHER_CURRENT_ASSETS", range(1261, 1300)),
    ("HELD_FOR_OTHER_BRANCHES", "2190", "Held for Other Branches", "LIABILITY", "2000", "CREDIT",
     "TRADE_PAYABLES", range(2191, 2200)),
)


def add_inter_branch_accounts(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")

    for key, code, name, account_type, header, normal, ifrs, free in _ACCOUNTS:
        headers = Account.objects.filter(code=header).values_list("entity_id", "pk")
        for entity_id, header_id in headers.iterator():
            if FinanceAccountMapping.objects.filter(entity_id=entity_id, key=key).exists():
                continue
            if Account.objects.filter(entity_id=entity_id, code=code, name=name).exists():
                continue
            taken = set(
                Account.objects.filter(entity_id=entity_id, code__startswith=code[:2])
                .values_list("code", flat=True)
            )
            chosen = code if code not in taken else next(
                (str(n) for n in free if str(n) not in taken), None)
            if chosen is None:
                continue
            account = Account.objects.create(
                entity_id=entity_id, code=chosen, name=name, account_type=account_type,
                is_postable=True, is_contra=False, is_active=True, normal_balance=normal,
                ifrs_line=ifrs, parent_id=header_id,
            )
            if chosen != code:
                FinanceAccountMapping.objects.create(entity_id=entity_id, key=key, account=account)


def remove_inter_branch_accounts(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")
    JournalLine = apps.get_model("vs_finance", "JournalLine")

    used = set(JournalLine.objects.values_list("account_id", flat=True).distinct())
    for key, code, name, _type, _header, _normal, _ifrs, _free in _ACCOUNTS:
        FinanceAccountMapping.objects.filter(key=key, account__name=name).exclude(
            account_id__in=used).delete()
        Account.objects.filter(name=name, code__startswith=code[:2]).exclude(pk__in=used).exclude(
            finance_mapping_roles__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0052_inter_branch_transfers"),
    ]

    operations = [
        migrations.RunPython(add_inter_branch_accounts, remove_inter_branch_accounts),
    ]
