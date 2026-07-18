from decimal import Decimal

from django.db import migrations, models


def backfill_expected_quantities(apps, schema_editor):
    PurchaseOrderLine = apps.get_model("vs_procurement", "PurchaseOrderLine")
    GoodsReceivedNoteLine = apps.get_model("vs_procurement", "GoodsReceivedNoteLine")

    for po_line in PurchaseOrderLine.objects.all().iterator():
        remaining = Decimal(po_line.quantity)
        receipt_lines = GoodsReceivedNoteLine.objects.filter(
            po_line_id=po_line.id,
        ).select_related("grn").order_by("grn__created_at", "grn_id", "id")
        for receipt_line in receipt_lines:
            inspected = Decimal(receipt_line.accepted_qty) + Decimal(receipt_line.rejected_qty)
            # Preserve the PO remainder at creation; malformed legacy rows still need a denominator large enough for delivery.
            receipt_line.expected_qty = max(remaining, inspected)
            receipt_line.save(update_fields=["expected_qty"])
            if receipt_line.grn.status == "POSTED":
                # Only accepted units advance PO fulfilment; rejected units remain available for a later receipt.
                remaining = max(Decimal(0), remaining - Decimal(receipt_line.accepted_qty))


class Migration(migrations.Migration):
    dependencies = [("vs_procurement", "0004_purchaseorder_delivery_terms")]

    operations = [
        migrations.AddField(
            model_name="goodsreceivednoteline",
            name="expected_qty",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text="PO quantity remaining when this receipt was created.",
                max_digits=14,
            ),
        ),
        migrations.RunPython(backfill_expected_quantities, migrations.RunPython.noop),
    ]
