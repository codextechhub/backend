"""A credit or debit note's kind reads "Credit note" or "Debit note" to a person.

The labels carried an accounting note ("reduces AR") into every message and
screen that named a note's kind. Choice labels only: no column or row changes.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0065_returned_document_corrections"),
    ]

    operations = [
        migrations.AlterField(
            model_name="creditnote",
            name="kind",
            field=models.CharField(
                choices=[("CREDIT", "Credit note"), ("DEBIT", "Debit note")],
                default="CREDIT",
                max_length=6,
            ),
        ),
    ]
