from django.db import migrations
from django.db.models import Sum


def backfill_receipt_values(apps, schema_editor):
    GoodsReceivedNote = apps.get_model("vs_procurement", "GoodsReceivedNote")
    GoodsReceivedNoteLine = apps.get_model("vs_procurement", "GoodsReceivedNoteLine")

    for line in GoodsReceivedNoteLine.objects.all().iterator():
        # Line extension uses accepted units only; rejected units never enter receipt value or the GL.
        calculated = int(line.accepted_qty * line.unit_price)
        if line.value_amount != calculated:
            line.value_amount = calculated
            line.save(update_fields=["value_amount"])

    for receipt in GoodsReceivedNote.objects.all().iterator():
        # Header preview is the sum of corrected line extensions, matching runtime recompute_total().
        total = receipt.lines.aggregate(value=Sum("value_amount"))["value"] or 0
        if receipt.total_value != total:
            receipt.total_value = total
            receipt.save(update_fields=["total_value"])


class Migration(migrations.Migration):
    dependencies = [("vs_procurement", "0005_grn_line_expected_quantity")]

    operations = [
        migrations.RunPython(backfill_receipt_values, migrations.RunPython.noop),
    ]
