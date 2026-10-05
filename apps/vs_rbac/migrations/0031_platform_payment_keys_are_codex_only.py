"""The payment keys that reach across every tenant are CodeX's alone.

Three payments resources span every tenant: webhooks that matched no tenant
(``payments.unattributed_webhook``), the money CodeX holds for every client
branch (``payments.platform_settlement``) and CodeX's own merchant account at
the provider (``payments.platform_provider``). Their views already require CodeX
staff, but the keys were registered ``TENANT``. Finance Admin owns every
``payments.`` key by prefix, so each school's Finance Admin was copied the
unattributed-webhook keys, and its roles screen offered a reach into every other
school's unmatched payments that only the view's own check withheld.

They become ``PLATFORM`` here, and every tenant-side grant of them is withdrawn
in the same step (:func:`vs_rbac.scope_withdrawal.withdraw_from_tenants`), so no
school role is left carrying a key the grant guard would then refuse. CodeX's
own roles keep them. The seed registers them ``PLATFORM`` from now on.

Reversing makes them tenant-holdable again and gives back nothing withdrawn.
"""
from django.db import migrations

from vs_rbac.scope_withdrawal import withdraw_from_tenants

RESOURCES = ("unattributed_webhook", "platform_settlement", "platform_provider")


def _keys(Permission):
    """The registered keys of the three resources (none before the seed has run)."""
    return [
        key for key in Permission.objects.filter(key__startswith="payments.")
        .values_list("key", flat=True)
        if key.split(".")[1] in RESOURCES
    ]


def codex_only(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    keys = _keys(Permission)
    if not keys:
        return
    Permission.objects.filter(key__in=keys).update(scope="PLATFORM")
    withdraw_from_tenants(keys, apps=apps)


def tenant_holdable_again(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    Permission.objects.filter(key__in=_keys(Permission)).update(scope="TENANT")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0030_a_teacher_reads_colleagues_by_relationship"),
    ]
    operations = [migrations.RunPython(codex_only, tenant_holdable_again)]
