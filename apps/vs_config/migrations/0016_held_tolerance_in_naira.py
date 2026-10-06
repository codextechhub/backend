"""The held-ledger reconciliation tolerance is set in naira, as every amount a person types is.

The setting was ``payments.held_reconciliation_tolerance_kobo``, a whole number of
kobo typed straight into the console, under a description that said "Kobo". A
platform operator reading money in naira would type 500 meaning ₦500 and set ₦5.
It becomes ``payments.held_reconciliation_tolerance``, a whole number of naira, and
:func:`vs_payments.held_reconciliation.tolerance` turns it into kobo where the
check compares it.

The definition row is renamed in place, so the values set for it and their audit
history stay attached. Each value set is converted from kobo to naira, rounded up
so no tolerance becomes stricter than it was (a tolerance of 150 kobo becomes ₦2).
Reversible: the key, description and values go back to kobo.
"""

from django.db import migrations

OLD_KEY = "payments.held_reconciliation_tolerance_kobo"
NEW_KEY = "payments.held_reconciliation_tolerance"
LABEL = "Held-ledger Reconciliation Tolerance"
NAIRA_DESCRIPTION = (
    "Naira by which the payment provider's reported balance may differ from the "
    "platform's books before the daily check opens a health incident. A whole "
    "number of naira; 0 means the two must agree exactly."
)
KOBO_DESCRIPTION = (
    "Kobo the payment provider's reported balance may differ from the platform's books "
    "before the daily check opens a health incident."
)


def _to_naira(value):
    try:
        kobo = max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
    return -(-kobo // 100)


def _to_kobo(value):
    try:
        return max(0, int(value or 0)) * 100
    except (TypeError, ValueError):
        return 0


def forwards(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    definition = definition_model.objects.filter(key=OLD_KEY).first()
    if definition is None:
        return
    definition.key = NEW_KEY
    definition.label = LABEL
    definition.description = NAIRA_DESCRIPTION
    definition.default_value = _to_naira(definition.default_value)
    definition.validation_rules = {"min": 0, "max": 1_000_000}
    definition.save()
    for row in value_model.objects.filter(definition=definition):
        row.value = _to_naira(row.value)
        row.save(update_fields=["value"])


def backwards(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    definition = definition_model.objects.filter(key=NEW_KEY).first()
    if definition is None:
        return
    definition.key = OLD_KEY
    definition.description = KOBO_DESCRIPTION
    definition.default_value = _to_kobo(definition.default_value)
    definition.validation_rules = {"min": 0, "max": 100_000_000}
    definition.save()
    for row in value_model.objects.filter(definition=definition):
        row.value = _to_kobo(row.value)
        row.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_config", "0015_seed_record_retention"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
