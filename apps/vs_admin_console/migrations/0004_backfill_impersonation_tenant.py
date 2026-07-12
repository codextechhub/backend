from django.db import migrations


def forwards(apps, schema_editor):
    Session = apps.get_model("vs_admin_console", "ImpersonationSession")
    for session in Session.objects.select_related("school").filter(tenant__isnull=True).iterator():
        Session.objects.filter(pk=session.pk).update(tenant_id=session.school.tenant_id)


def backwards(apps, schema_editor):
    apps.get_model("vs_admin_console", "ImpersonationSession").objects.update(tenant=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_admin_console", "0003_impersonationsession_tenant_and_more"),
        ("vs_tenants", "0002_backfill_tenants"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
