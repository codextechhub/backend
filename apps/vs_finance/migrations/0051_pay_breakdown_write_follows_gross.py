"""The pay breakdown's write switch, for the roles that may already change pay.

``finance.salary.components`` (Pay breakdown) governs a person's voluntary
deductions, the salary structure assigned to them and the lines of a salary
structure. It is writable, and every payroll and salary write checks it, so a
role without its write switch can no longer change any of those.

A role that may read the pay breakdown and change a person's gross pay has
been setting deductions and structures all along. Its write switch on the
breakdown is turned on here, so the check takes nothing from it. Every other
role keeps the breakdown read-only until an administrator decides otherwise:
a role trusted to read pay but not change it gains nothing.

``manage.py sync_field_registry``, which runs in the deploy sequence after the
migrations, marks the field writable; until it runs the switch set here has no
effect. Reversing makes the breakdown read-only for every role again, as it was
before the field was writable.
"""
from django.db import migrations

BREAKDOWN = "finance.salary.components"
GROSS = "finance.salary.gross_amount"


def _gross_writers(RoleFieldAccess):
    return RoleFieldAccess.objects.filter(
        field_id=GROSS, can_read=True, can_write=True,
    ).values("role_id")


def breakdown_write_follows_gross(apps, schema_editor):
    RoleFieldAccess = apps.get_model("vs_rbac", "RoleFieldAccess")
    RoleFieldAccess.objects.filter(
        field_id=BREAKDOWN, can_read=True, can_write=False,
        role_id__in=_gross_writers(RoleFieldAccess),
    ).update(can_write=True)


def breakdown_read_only_again(apps, schema_editor):
    RoleFieldAccess = apps.get_model("vs_rbac", "RoleFieldAccess")
    RoleFieldAccess.objects.filter(field_id=BREAKDOWN, can_write=True).update(can_write=False)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0050_one_active_roster_row_per_person"),
        ("vs_rbac", "0030_a_teacher_reads_colleagues_by_relationship"),
    ]

    operations = [
        migrations.RunPython(breakdown_write_follows_gross, breakdown_read_only_again),
    ]
