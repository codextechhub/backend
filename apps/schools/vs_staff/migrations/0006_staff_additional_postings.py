from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_staff", "0005_staff_records_for_accounts_that_predate_them")]

    operations = [
        migrations.AddField(
            model_name="staffprofile",
            name="additional_postings",
            field=models.ManyToManyField(
                blank=True,
                related_name="additional_staff_postings",
                to="vs_tenants.branch",
            ),
        ),
    ]
