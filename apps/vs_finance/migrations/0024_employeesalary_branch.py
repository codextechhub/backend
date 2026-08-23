"""Give the salary roster a branch, so payroll can be run per branch.

Nullable and **not** backfilled, on purpose. Every existing row stays null, which
is the honest state: no school has assigned anybody to a branch for payroll
purposes yet, and null means exactly that here (an unassigned row) rather than
"shared across the school" as it does elsewhere in finance.

Nothing changes behaviour on its own. ``payroll.scope`` defaults to CENTRAL, a
central run covers the whole roster whatever the branch column says, and a school
cannot switch to per-branch payroll until every active row has been assigned.

Reversible: dropping the column loses only assignments, and CENTRAL - which is
every school until one opts in - never reads it.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0023_alter_financeauditlog_action_financedocumentdelivery"),
        ("vs_tenants", "0008_alter_branch_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="employeesalary",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch this person is on the payroll of. Empty means not yet assigned; per-branch payroll cannot be switched on while any active employee is unassigned.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_employee_salaries",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddIndex(
            model_name="employeesalary",
            index=models.Index(
                fields=["entity", "branch", "is_active"],
                name="vs_finance__entity__f422f4_idx",
            ),
        ),
    ]
