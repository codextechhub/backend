import re

from django.db import migrations, models


def backfill_number_codes(apps, schema_editor):
    """Give every existing entity a unique short code derived from its `code`."""
    LedgerEntity = apps.get_model("vs_finance", "LedgerEntity")
    taken = set()
    for entity in LedgerEntity.objects.order_by("pk"):
        base = re.sub(r"[^A-Z0-9]", "", (entity.code or "").upper())[:3] or "ENT"
        code = base
        if code in taken:
            stem = base[:2]
            for suffix in "23456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                candidate = (stem + suffix)[:3]
                if candidate not in taken:
                    code = candidate
                    break
        entity.number_code = code
        entity.save(update_fields=["number_code"])
        taken.add(code)


class Migration(migrations.Migration):
    dependencies = [("vs_finance", "0005_seed_platform_entity")]

    operations = [
        # 1) Add nullable-blank first so existing rows don't violate uniqueness.
        migrations.AddField(
            model_name="ledgerentity",
            name="number_code",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
        # 2) Backfill a unique short code for every existing entity.
        migrations.RunPython(backfill_number_codes, migrations.RunPython.noop),
        # 3) Now that all rows are populated + unique, enforce it at the DB level.
        migrations.AlterField(
            model_name="ledgerentity",
            name="number_code",
            field=models.CharField(
                blank=True, default="", max_length=3, unique=True,
                help_text="2–3 char code embedded in document numbers (e.g. CDX). "
                          "Auto-derived from `code` when left blank; kept globally unique.",
            ),
        ),
    ]
