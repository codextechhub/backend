from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vs_tickets", "0003_ticket_number_per_tenant_per_day")]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="context",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
