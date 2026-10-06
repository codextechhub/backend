"""Stored money-filter bounds move from kobo to naira.

A money filter's ``min`` and ``max`` are naira: :func:`vs_exports.catalogue.compile_filter`
converts them to kobo when the filter is applied. Bounds saved while the filter
read kobo would otherwise be read a hundred times too large, so this migration
divides them by 100 once, and every saved recipe keeps finding the same rows.

**What is rewritten.** ``ExportDefinition.filters`` (saved recipes, which
schedules run) and the ``filters`` inside every ``ExportRun.frozen_config``,
queued or finished. A queued run is executed from its frozen configuration, so
it must read naira too. A finished one is rewritten as well, because the run
detail reads its frozen filters back as a sentence and compares them with the
recipe to report drift: left in kobo, every past run would read as filtering
on ₦5,000,000 and as differing from its own unchanged recipe. The unit changes;
the rows it describes do not.

**What is not.** Audit events that recorded a recipe's filters before an edit
are immutable history and stay as written. Export schedules hold no filters of
their own.

**Which filters are money** is frozen below rather than read from the live
catalogue: a data migration replays against the schema and code of its own
time, and the catalogue may later gain, drop or rename a filter. These are the
number-range filters over a money column that existed when the unit changed.

A whole-naira bound is stored as an integer, as the builder sends it; one with
kobo is stored as an exact decimal string (``"50000.50"``), which the
catalogue reads the same way, rather than a float. The reverse multiplies back
to integer kobo.
"""
from decimal import Decimal

from django.db import migrations

#: ``dataset_key -> ids of its money filters``, as they stood at this migration.
MONEY_FILTERS = {
    "finance.customer_invoices": {"total"},
    "finance.expense_claims": {"total"},
}

BOUNDS = ("min", "max")


# Kobo, as stored before, to naira as the builder sends it.
def _to_naira(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return value
    try:
        kobo = Decimal(str(value))
    except ArithmeticError:
        return value
    naira = kobo / 100
    if naira == naira.to_integral_value():
        return int(naira)
    return f"{naira.quantize(Decimal('0.01'))}"


# Naira back to integer kobo.
def _to_kobo(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return value
    try:
        naira = Decimal(str(value))
    except ArithmeticError:
        return value
    return int((naira * 100).to_integral_value())


# Convert the money bounds in one filter list; True when anything changed.
def _convert_filters(dataset_key, filters, convert):
    money = MONEY_FILTERS.get(dataset_key)
    if not money or not isinstance(filters, list):
        return False
    changed = False
    for spec in filters:
        if not isinstance(spec, dict) or spec.get("id") not in money:
            continue
        for bound in BOUNDS:
            if spec.get(bound) in (None, ""):
                continue
            new = convert(spec[bound])
            if new != spec[bound] or type(new) is not type(spec[bound]):
                spec[bound] = new
                changed = True
    return changed


def _rewrite(apps, convert):
    Definition = apps.get_model("vs_exports", "ExportDefinition")
    Run = apps.get_model("vs_exports", "ExportRun")
    keys = list(MONEY_FILTERS)

    for definition in Definition.objects.filter(dataset_key__in=keys).only(
        "pk", "dataset_key", "filters",
    ).iterator():
        if _convert_filters(definition.dataset_key, definition.filters, convert):
            Definition.objects.filter(pk=definition.pk).update(filters=definition.filters)

    for run in Run.objects.filter(frozen_config__dataset_key__in=keys).only(
        "pk", "frozen_config",
    ).iterator():
        config = run.frozen_config or {}
        if _convert_filters(config.get("dataset_key"), config.get("filters"), convert):
            Run.objects.filter(pk=run.pk).update(frozen_config=config)


def forwards(apps, schema_editor):
    _rewrite(apps, _to_naira)


def backwards(apps, schema_editor):
    _rewrite(apps, _to_kobo)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_exports", "0005_exportdownload_access_kind"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
