from django.core.validators import MaxValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_finance", "0015_alter_financeauditlog_action_financebankingsettings")]

    operations = [
        migrations.AddField(
            model_name="financebankingsettings",
            name="petty_cash_low_balance_threshold_bps",
            field=models.PositiveSmallIntegerField(
                default=2500,
                help_text="Petty-cash balance percentage that triggers a replenishment alert.",
                validators=[MaxValueValidator(10000)],
            ),
        ),
    ]
