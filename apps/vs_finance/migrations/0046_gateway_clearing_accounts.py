"""Give existing books a gateway clearing account.

Every set of books whose chart was seeded (it holds the ``1000`` assets header) is
given the account new books are seeded with: 1125 Gateway Clearing, an asset under
1000, where a confirmed online payment waits until its settlement reaches a bank.

Books that already hold it (a chart seeded after the account joined the starter
chart) are left alone. Where the books already use 1125 for something else, the
account is created at the first free code from 1126 to 1199 instead, and the gateway
clearing role is mapped to it, so the role never resolves to the tenant's own account
that happens to carry 1125. Books with no free code in that range are left for an
administrator to map.

The reverse removes the mappings this made and the accounts nothing has posted to.
"""
from django.db import migrations

_CODE = "1125"
_NAME = "Gateway Clearing"
_KEY = "GATEWAY_CLEARING"


def forwards(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")

    headers = Account.objects.filter(code="1000").values_list("entity_id", "pk")
    for entity_id, header_id in headers.iterator():
        if FinanceAccountMapping.objects.filter(entity_id=entity_id, key=_KEY).exists():
            continue
        if Account.objects.filter(entity_id=entity_id, code=_CODE, name=_NAME).exists():
            continue
        taken = set(
            Account.objects.filter(entity_id=entity_id, code__startswith="11")
            .values_list("code", flat=True)
        )
        if _CODE not in taken:
            code = _CODE
        else:
            code = next((str(n) for n in range(1126, 1200) if str(n) not in taken), None)
            if code is None:
                continue
        account = Account.objects.create(
            entity_id=entity_id, code=code, name=_NAME, account_type="ASSET",
            is_postable=True, is_contra=False, is_active=True, normal_balance="DEBIT",
            ifrs_line="CASH", parent_id=header_id,
        )
        if code != _CODE:
            FinanceAccountMapping.objects.create(entity_id=entity_id, key=_KEY, account=account)


def backwards(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    FinanceAccountMapping = apps.get_model("vs_finance", "FinanceAccountMapping")
    JournalLine = apps.get_model("vs_finance", "JournalLine")

    FinanceAccountMapping.objects.filter(key=_KEY, account__name=_NAME).delete()
    used = set(JournalLine.objects.values_list("account_id", flat=True).distinct())
    Account.objects.filter(name=_NAME, code__startswith="11").exclude(pk__in=used).exclude(
        finance_mapping_roles__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0045_collection_account_per_branch"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
