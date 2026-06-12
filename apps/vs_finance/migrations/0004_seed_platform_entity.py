"""Seed Codex's own platform set of books.

Creates the single ``CODEX`` platform :class:`LedgerEntity` so internal CX_STAFF
finance staff have real, isolated books from day one — entirely separate from any
tenant's ledger. Idempotent and reversible. Depends on 0003 because
``base_currency`` is a protected FK onto the seeded NGN currency.
"""
from django.db import migrations

PLATFORM_ENTITY_CODE = "CODEX"


def create_platform_entity(apps, schema_editor):
    LedgerEntity = apps.get_model("vs_finance", "LedgerEntity")
    LedgerEntity.objects.update_or_create(
        code=PLATFORM_ENTITY_CODE,
        defaults={
            "name": "CodeX",
            "kind": "PLATFORM",
            "source_school": None,
            "base_currency_id": "NGN",
            "is_active": True,
        },
    )


def remove_platform_entity(apps, schema_editor):
    LedgerEntity = apps.get_model("vs_finance", "LedgerEntity")
    LedgerEntity.objects.filter(code=PLATFORM_ENTITY_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0003_seed_currencies"),
    ]

    operations = [
        migrations.RunPython(create_platform_entity, remove_platform_entity),
    ]
