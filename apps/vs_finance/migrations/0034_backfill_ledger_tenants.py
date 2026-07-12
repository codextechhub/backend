from django.db import migrations


def forwards(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    Entity = apps.get_model("vs_finance", "LedgerEntity")
    codex = Tenant.objects.get(slug="codex", kind="PLATFORM")
    for entity in Entity.objects.select_related("source_school").all().iterator():
        tenant_id = getattr(entity.source_school, "tenant_id", None) or codex.pk
        Entity.objects.filter(pk=entity.pk).update(tenant_id=tenant_id)


def backwards(apps, schema_editor):
    apps.get_model("vs_finance", "LedgerEntity").objects.update(tenant=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0033_ledgerentity_tenant_and_more"),
        ("vs_tenants", "0002_backfill_tenants"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
