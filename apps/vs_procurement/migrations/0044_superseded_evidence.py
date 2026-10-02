"""Evidence on a bill or payment that has left draft is superseded, never deleted.

Adds ``superseded_at``, ``superseded_by`` and ``superseded_reason`` to both
attachment tables (:func:`vs_procurement.attachments.remove_attachment`). Every
existing file reads as current. Reversible.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0043_stock_transfers"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorinvoiceattachment",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendorinvoiceattachment",
            name="superseded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="vendor_invoice_attachments_superseded",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="vendorinvoiceattachment",
            name="superseded_reason",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="vendorpaymentattachment",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendorpaymentattachment",
            name="superseded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="vendor_payment_attachments_superseded",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="vendorpaymentattachment",
            name="superseded_reason",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
