import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_user", "0003_user_tenant_persona"),
        ("vs_tenants", "0002_backfill_tenants"),
    ]
    operations = [
        migrations.AlterField(
            model_name="user",
            name="tenant",
            field=models.ForeignKey(help_text="Canonical home tenant.", on_delete=django.db.models.deletion.PROTECT, related_name="users", to="vs_tenants.tenant"),
        ),
    ]
