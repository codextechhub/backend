import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_schools", "0002_remove_schoolpackagesetup_enabled_modules_and_more"),
        ("vs_tenants", "0001_initial"),
    ]
    operations = [
        migrations.AddField(
            model_name="school",
            name="tenant",
            field=models.OneToOneField(blank=True, help_text="Canonical ownership boundary; temporarily nullable during backfill.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="school_profile", to="vs_tenants.tenant"),
        ),
    ]
