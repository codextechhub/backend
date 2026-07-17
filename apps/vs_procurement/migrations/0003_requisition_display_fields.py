import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaserequisition",
            name="title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="purchaserequisitionline",
            name="catalog_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="requisition_lines",
                to="vs_procurement.catalogitem",
            ),
        ),
        migrations.AddField(
            model_name="purchaserequisitionline",
            name="unit",
            field=models.CharField(blank=True, default="Unit", max_length=24),
        ),
    ]
