"""Name the impersonated person on a finance audit row taken under a proxy.

The row's ``actor`` is the real person; ``effective_user`` is whom they acted
as. Existing rows are not backfilled: the table's immutability triggers refuse
any UPDATE, and those rows keep the pair in ``metadata['effective_user_id']``.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0031_fiscal_calendar_settings"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="financeauditlog",
            name="effective_user",
            field=models.ForeignKey(
                blank=True,
                help_text="The person being impersonated when the action was proxied; null otherwise.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_audit_events_as_proxied",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
