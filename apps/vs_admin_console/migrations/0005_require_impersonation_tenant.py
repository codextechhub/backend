import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_admin_console", "0004_backfill_impersonation_tenant")]
    operations = [
        migrations.AlterField(
            model_name="impersonationsession",
            name="tenant",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="impersonation_sessions", to="vs_tenants.tenant"),
        ),
    ]
