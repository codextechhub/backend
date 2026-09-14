"""Sweep up the grants that three reclassifications left standing.

Making a key ``PLATFORM`` decides who may hold it next. The rows already
written are a separate job, and it was done differently every time: 0008 swept
nothing, 0018 swept a school's own roles, 0019 swept those and the prebuilt
library. Nobody swept tenant permission groups, and no sweep looked past
``kind="SCHOOL"`` at an organization tenant.

Two survivors were sitting in this database:

* ``finance.entity.create`` on the Finance Admin template in the prebuilt
  library. Creating a school provisions Finance Admin by copying that library
  row into the school's own role, and the scope guard refused the copy, so
  **creating a school failed outright** with "Permission(s)
  finance.entity.create are platform-scoped and cannot be granted inside a
  tenant". The key had been platform-only since 0018; the library kept handing
  it out.

* ``import.templates.create`` inside the tenant-scoped groups "Data Import -
  all" and "Import Template - all", left by 0008. Neither group is attached to
  a role yet, and attaching either would have been refused with the same kind
  of error, naming the group instead of the permission.

The sweep is driven from ``Permission.scope`` rather than from a list of keys,
so it takes back whatever any past reclassification left behind rather than
only the two known instances. It is the same code a future reclassification
calls: :func:`vs_rbac.scope_withdrawal.withdraw_from_tenants`.

Irreversible by design. These grants confer nothing - the evaluator already
filters non-tenant keys out for a tenant that is not the platform - so putting
them back would restore the breakage and no access.
"""
from django.db import migrations

from vs_rbac.scope_withdrawal import withdraw_from_tenants


def sweep(apps, schema_editor):
    withdraw_from_tenants(apps=apps)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0020_a_renamed_branch_takes_its_roles_with_it"),
    ]
    operations = [
        migrations.RunPython(sweep, migrations.RunPython.noop),
    ]
