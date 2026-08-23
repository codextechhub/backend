"""Seed the one Codex PLATFORM tenant.

The platform tenant is load-bearing infrastructure, not fixture data: CX user
creation derives its home tenant from it, every platform permission seed grants
into codex-owned roles, and the test suite builds its database from this chain.
Idempotent and reversible (the reverse refuses if codex already owns rows).
"""
from django.db import migrations
from django.utils import timezone


CODEX_SLUG = "codex"


def forwards(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    Tenant.objects.get_or_create(
        slug=CODEX_SLUG,
        defaults={
            "name": "CodeX",
            "kind": "PLATFORM",
            "status": "ACTIVE",
            "activated_at": timezone.now(),
        },
    )


def backwards(apps, schema_editor):
    apps.get_model("vs_tenants", "Tenant").objects.filter(
        slug=CODEX_SLUG, kind="PLATFORM",
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("vs_tenants", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
