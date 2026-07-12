import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_schools", "0003_school_tenant"),
        ("vs_tenants", "0002_backfill_tenants"),
    ]
    operations = [
        migrations.AlterField(
            model_name="school",
            name="tenant",
            field=models.OneToOneField(help_text="Canonical ownership boundary.", on_delete=django.db.models.deletion.PROTECT, related_name="school_profile", to="vs_tenants.tenant"),
        ),
    ]
