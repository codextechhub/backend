"""One active salary roster row per person.

Each member of staff is paid in full by one branch, so a person has at most one
active :class:`~vs_finance.models.EmployeeSalary` row. PAYE is worked out per
row, and a person split across two rows has each part taxed as if it were their
whole pay.

The rule is a partial unique index on ``employee`` over active rows with a
person linked. A database that already holds two active rows for one person
cannot take that index, and neither failing the deploy nor choosing which row
survives is acceptable: the rows carry real pay history, and merging them is an
administrator's decision. So the index is created only where no duplicate
exists. Where one does, the migration records the constraint in Django's state,
leaves the index out, and says so; the application's own check
(:func:`vs_finance.payroll_statutory.assert_one_active_row`) refuses any new
duplicate either way, and ``manage.py report_duplicate_roster_rows`` lists the
existing ones and creates the index once they are resolved.

Reversing drops the index if it is there.
"""
from django.db import migrations, models

CONSTRAINT = models.UniqueConstraint(
    fields=["employee"],
    condition=models.Q(is_active=True, employee__isnull=False),
    name="uniq_active_roster_row_per_person",
)

DUPLICATES_SQL = """
    SELECT employee_id FROM vs_finance_employeesalary
    WHERE is_active AND employee_id IS NOT NULL
    GROUP BY employee_id HAVING COUNT(*) > 1
    LIMIT 1
"""


def install_if_clean(apps, schema_editor):
    """Create the index unless a person already has two active rows."""
    model = apps.get_model("vs_finance", "EmployeeSalary")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DUPLICATES_SQL)
        duplicate = cursor.fetchone()
    if duplicate is not None:
        print(
            "\n  Some people have more than one active salary roster row, so the "
            "one-row-per-person index was not created. Run "
            "'manage.py report_duplicate_roster_rows' to list them; it creates the "
            "index once they are resolved."
        )
        return
    schema_editor.add_constraint(model, CONSTRAINT)


def drop_if_present(apps, schema_editor):
    schema_editor.execute(f'DROP INDEX IF EXISTS "{CONSTRAINT.name}"')


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0049_statutory_payroll_data"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(model_name="employeesalary", constraint=CONSTRAINT),
            ],
            database_operations=[
                migrations.RunPython(install_if_clean, drop_if_present),
            ],
        ),
    ]
