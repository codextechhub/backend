"""Seed the default currencies (NGN base, plus USD/GBP/EUR).

Reference data shared by every entity; idempotent and reversible. The Chart of
Accounts is intentionally *not* seeded here — it is per-entity and created on demand
via ``manage.py seed_finance`` so tenants opt in to a starter chart explicitly.

Must run BEFORE 0004_seed_platform_entity: LedgerEntity.base_currency is a
protected FK onto Currency, so the NGN row has to exist first.
"""
from django.db import migrations

DEFAULT_CURRENCIES = [
    {"code": "NGN", "name": "Nigerian Naira", "symbol": "₦", "minor_unit": 2},
    {"code": "USD", "name": "US Dollar", "symbol": "$", "minor_unit": 2},
    {"code": "GBP", "name": "Pound Sterling", "symbol": "£", "minor_unit": 2},
    {"code": "EUR", "name": "Euro", "symbol": "€", "minor_unit": 2},
]


def create_currencies(apps, schema_editor):
    Currency = apps.get_model("vs_finance", "Currency")
    for spec in DEFAULT_CURRENCIES:
        Currency.objects.update_or_create(code=spec["code"], defaults=spec)


def remove_currencies(apps, schema_editor):
    Currency = apps.get_model("vs_finance", "Currency")
    Currency.objects.filter(code__in=[c["code"] for c in DEFAULT_CURRENCIES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0003_financeauditlog_immutability_triggers"),
    ]

    operations = [
        migrations.RunPython(create_currencies, remove_currencies),
    ]
