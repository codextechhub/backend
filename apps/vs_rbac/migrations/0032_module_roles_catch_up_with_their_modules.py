"""Each tenant's Finance Admin and Procurement Admin gain the keys shipped since it opened.

The library roles own their modules by prefix (finance and payments for Finance
Admin, procurement for Procurement Admin), and a tenant's copy is taken when the
tenant is created. Nothing carried a key registered later into those copies, so
every school created before a feature shipped had a Finance Admin who could not
use it: cutting and closing a petty cash float, reopening a fiscal year,
inter-branch transfers, bank transactions and transfers, deposits, deferred
income, doubtful-debt provisions, payment settings, among others.

This is the catch-up. Every tenant copy of those two roles is granted each
tenant-scoped key under its role's prefixes that was registered after the copy
was created and that it holds no row for. Such a key was never on the copy, so
its absence is the gap and not the school's decision; a key the school refused
keeps its deny row. From here on the seed grows the copies as the library grows
(:mod:`vs_rbac.library_growth`).

A fresh database has no tenant roles yet, so this does nothing there. The prefix
map is frozen here as it stands, rather than read from the seed, so a later
change to the seed cannot change what this migration did.

Reversing is a no-op: it could not tell these grants from ones an administrator
made afterwards.
"""
from django.db import migrations

from vs_rbac.library_growth import grant_to_copies

PREFIXES = {
    "finance_admin": ("finance.", "payments."),
    "procurement_admin": ("procurement.",),
}


def catch_up(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    for prebuilt_key, prefixes in PREFIXES.items():
        keys = [
            key for key in Permission.objects.filter(scope="TENANT", is_active=True)
            .values_list("key", flat=True)
            if key.startswith(prefixes)
        ]
        grant_to_copies(prebuilt_key, keys, apps=apps, registered_after_role=True)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0031_platform_payment_keys_are_codex_only"),
    ]
    operations = [migrations.RunPython(catch_up, migrations.RunPython.noop)]
