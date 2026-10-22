"""An expense claim's claimant is picked from the staff list, or typed for somebody not on it.

The link to the claimant's account already exists; this restates what the two
columns mean. No claim is changed: every claim raised before the staff list
was offered keeps its typed name and no link. A claim is never linked by
matching its name, because a typed name can match a pupil's or a parent's
account as easily as a member of staff's, and a wrong link would hand that
person the claim's receipts. Reversible both ways with nothing to undo.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0075_delivery_recipient_contacts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="expenseclaim",
            name="claimant",
            field=models.ForeignKey(
                blank=True,
                help_text="The member of staff being reimbursed; empty for somebody not on the staff list.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_expense_claims",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="expenseclaim",
            name="claimant_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The name the claim reads under: copied from the claimant when one is linked, typed for somebody not on the staff list.",
                max_length=160,
            ),
        ),
    ]
