"""Give existing books the accounts and route the receivables accruals post through.

* **Accounts.** Every set of books whose chart was seeded (it holds the ``2000``
  liabilities header) is given the accounts new books are seeded with: 1290
  Allowance for Doubtful Debts, 2160 Deferred Income, 2170 Customer Deposits Held,
  4810 Bad Debts Recovered, 4820 Forfeited Deposits and 5350 Bad Debts. A code the
  books already use is left as it is: the mapping screen points the role at
  another account there.
* **Provision route.** Every tenant holding its own adjustment routes (the books
  publish them) is given an empty route for doubtful-debt provisions, as new books
  are. With no steps, a run cannot be submitted until the tenant adds approvers,
  and posting it directly needs the poster to confirm, which is recorded.

The reverse removes the empty routes and the accounts nothing has posted to.
"""
from django.db import migrations

_ACCOUNTS = (
    # (code, name, type, contra, parent, ifrs_line)
    ("1290", "Allowance for Doubtful Debts", "ASSET", True, "1000", "TRADE_RECEIVABLES"),
    ("2160", "Deferred Income", "LIABILITY", False, "2000", "DEFERRED_INCOME"),
    ("2170", "Customer Deposits Held", "LIABILITY", False, "2000", "TRADE_PAYABLES"),
    ("4810", "Bad Debts Recovered", "INCOME", False, "4000", "OTHER_INCOME"),
    ("4820", "Forfeited Deposits", "INCOME", False, "4000", "OTHER_INCOME"),
    ("5350", "Bad Debts", "EXPENSE", False, "5000", "ADMIN_EXPENSES"),
)
_NORMAL = {"ASSET": "DEBIT", "EXPENSE": "DEBIT", "LIABILITY": "CREDIT",
           "EQUITY": "CREDIT", "INCOME": "CREDIT"}
_FLIP = {"DEBIT": "CREDIT", "CREDIT": "DEBIT"}
_PROVISION_TYPE = "finance.doubtful_debt_provision"
_TEMPLATE_CODE = "standard"


def forwards(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")

    entity_ids = Account.objects.filter(code="2000").values_list("entity_id", flat=True)
    for entity_id in entity_ids.iterator():
        taken = set(Account.objects.filter(entity_id=entity_id).values_list("code", flat=True))
        parents = dict(
            Account.objects.filter(entity_id=entity_id, code__in=("1000", "2000", "4000", "5000"))
            .values_list("code", "pk")
        )
        for code, name, account_type, contra, parent, ifrs_line in _ACCOUNTS:
            if code in taken:
                continue
            normal = _NORMAL[account_type]
            Account.objects.create(
                entity_id=entity_id, code=code, name=name, account_type=account_type,
                is_postable=True, is_contra=contra, is_active=True,
                normal_balance=_FLIP[normal] if contra else normal,
                ifrs_line=ifrs_line, parent_id=parents.get(parent),
            )

    tenants = set(
        WorkflowTemplate.objects.filter(
            document_type="finance.write_off", branch__isnull=True, code=_TEMPLATE_CODE,
            tenant__isnull=False,
        ).values_list("tenant_id", flat=True)
    )
    for tenant_id in sorted(tenants):
        WorkflowTemplate.objects.get_or_create(
            tenant_id=tenant_id, branch=None, document_type=_PROVISION_TYPE,
            code=_TEMPLATE_CODE,
            defaults={
                "name": "Doubtful-debt provision approval",
                "description": (
                    "Approval route for a doubtful-debt provision run, held by this "
                    "tenant so its runs are never governed by shared platform rules. "
                    "The steps are the tenant's own to add."
                ),
            },
        )


def backwards(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    JournalLine = apps.get_model("vs_finance", "JournalLine")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")

    WorkflowTemplate.objects.filter(
        document_type=_PROVISION_TYPE, code=_TEMPLATE_CODE, branch__isnull=True,
    ).exclude(stages__isnull=False).delete()
    used = set(JournalLine.objects.values_list("account_id", flat=True).distinct())
    for code, name, *_rest in _ACCOUNTS:
        Account.objects.filter(code=code, name=name).exclude(pk__in=used).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0043_receivables_accruals"),
        ("vs_workflow", "0007_workflowtemplate_is_active_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
