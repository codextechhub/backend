"""Record the real person behind a gateway action taken under a proxy.

``PaymentEvent.actor_user`` is the person in whose name the action ran;
``proxied_by`` is who was at the keyboard when that was an impersonation
session. Earlier events carry no proxy context to derive it from, so they are
left empty.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0007_approver_groups_nobody_asked_for"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentevent",
            name="proxied_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
