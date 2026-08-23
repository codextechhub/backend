from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_procurement", "0018_procurementsettings_default_rfq_response_days_and_more")]

    operations = [
        migrations.AddField(
            model_name="procurementsettings",
            name="minimum_rfq_invited_vendors",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Minimum distinct vendors required before an RFQ can be issued.",
                validators=[MinValueValidator(1), MaxValueValidator(50)],
            ),
        ),
        migrations.AddField(
            model_name="procurementsettings",
            name="minimum_submitted_quotations_before_award",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Minimum submitted quotations required before an RFQ can be awarded.",
                validators=[MinValueValidator(1), MaxValueValidator(50)],
            ),
        ),
    ]
