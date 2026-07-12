import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_finance", "0034_backfill_ledger_tenants")]
    operations = [
        migrations.AlterField(
            model_name="ledgerentity",
            name="tenant",
            field=models.ForeignKey(help_text="Canonical owner.", on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entities", to="vs_tenants.tenant"),
        ),
    ]
