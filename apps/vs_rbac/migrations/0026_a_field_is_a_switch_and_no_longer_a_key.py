"""Delete the eight permission keys that only ever stood for a field.

Each of these guarded fields and nothing else: no view asks for one, no
service consults one, and since Field Access enforcement every field they
named is decided by the role's own Read and Write switches. A key that is
registered and granted while deciding nothing is worse than a missing one,
because ticking it on a role tells the person editing it she has granted
something she has not. Adaeze ticks "Read field-level sensitive data" for
Bright Star's nurse, saves, and the nurse still cannot see a blood group,
because the answer now lives on the Field Access screen she did not open.

The keys, and what each became, is :mod:`vs_rbac.field_conversion`:
``procurement.vendor.view_sensitive``, ``finance.bankaccount.view_sensitive``,
``finance.payrollrun.view_sensitive``,
``payments.virtual_account.view_sensitive``, ``payments.payout.view_sensitive``,
``school.students.view_sensitive``, ``platform.staff_payroll.view`` and
``platform.staff_payroll.manage``.

What goes with them
-------------------

Everything hanging off the row, in the order the foreign keys allow. Personal
exceptions and role-change delta items protect their permission, so they are
deleted first; grants, group memberships, prebuilt defaults and dependency
links would cascade, and are deleted explicitly so the count is visible rather
than implied. ``platform.staff_payroll`` loses both its actions, so the empty
resource goes too: an access catalogue that still listed it would offer a
branch with nothing under it.

A pending role change that asked for one of these keys loses that line. The
request survives with its other lines, which is the truthful outcome: the
thing it asked for no longer exists to be approved.

The registry revision is bumped, because an already-warm user object must not
keep authority the registry has just withdrawn.

What a reverse does not bring back
----------------------------------

Nothing. Reverse is a no-op, deliberately, in the same sense as migrations
0018 and 0019: re-creating a ``Permission`` row is trivial, but the grants,
group memberships, prebuilt defaults and personal exceptions that made it mean
something for a named person are gone, and inventing them would hand somebody
authority nobody granted.

One consequence is worth stating plainly. Migration 0025 finds the switches it
wrote by recomputing them from the keys a role holds. After this migration the
roles hold none of those keys, so reversing 0025 finds nothing and leaves every
converted switch in place. Recovering the old shape means restoring the
database, not running migrations backwards.
"""
from django.db import migrations, models

RETIRED_KEYS = [
    "procurement.vendor.view_sensitive",
    "finance.bankaccount.view_sensitive",
    "finance.payrollrun.view_sensitive",
    "payments.virtual_account.view_sensitive",
    "payments.payout.view_sensitive",
    "school.students.view_sensitive",
    "platform.staff_payroll.view",
    "platform.staff_payroll.manage",
]

#: The resource left with no actions at all once the keys above are gone.
EMPTIED_RESOURCE = ("platform", "staff_payroll")


def retire_them(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionResource = apps.get_model("vs_rbac", "PermissionResource")
    PermissionDependency = apps.get_model("vs_rbac", "PermissionDependency")
    GroupPermission = apps.get_model("vs_rbac", "GroupPermission")
    PrebuiltRolePermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")
    TenantRolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    UserPermissionOverride = apps.get_model("vs_rbac", "UserPermissionOverride")
    TenantRoleChangeDeltaItem = apps.get_model("vs_rbac", "TenantRoleChangeDeltaItem")
    PermissionRegistryRevision = apps.get_model("vs_rbac", "PermissionRegistryRevision")

    # The two that protect their permission, so the delete below can proceed.
    UserPermissionOverride.objects.filter(permission_id__in=RETIRED_KEYS).delete()
    TenantRoleChangeDeltaItem.objects.filter(permission_id__in=RETIRED_KEYS).delete()

    PermissionDependency.objects.filter(permission_id__in=RETIRED_KEYS).delete()
    PermissionDependency.objects.filter(depends_on_id__in=RETIRED_KEYS).delete()
    GroupPermission.objects.filter(permission_id__in=RETIRED_KEYS).delete()
    PrebuiltRolePermission.objects.filter(permission_id__in=RETIRED_KEYS).delete()
    TenantRolePermission.objects.filter(permission_id__in=RETIRED_KEYS).delete()

    Permission.objects.filter(key__in=RETIRED_KEYS).delete()

    module, resource = EMPTIED_RESOURCE
    emptied = PermissionResource.objects.filter(module_id=module, name=resource)
    if not Permission.objects.filter(
        module_id=module, resource__name=resource,
    ).exists():
        emptied.delete()

    PermissionRegistryRevision.objects.filter(pk=1).update(
        revision=models.F("revision") + 1,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0025_a_sensitive_key_becomes_a_field_switch"),
    ]
    operations = [
        migrations.RunPython(retire_them, migrations.RunPython.noop),
    ]
