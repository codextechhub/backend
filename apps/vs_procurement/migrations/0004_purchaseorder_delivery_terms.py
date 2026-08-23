from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_procurement", "0003_requisition_display_fields")]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="delivery_address",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="payment_terms",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
    ]
