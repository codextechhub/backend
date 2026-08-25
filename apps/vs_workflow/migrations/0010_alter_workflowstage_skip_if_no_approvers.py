"""An unstaffed stage parks by default instead of passing the document on.

``skip_if_no_approvers`` defaulted to True, so a stage published without the
field auto-skipped when nobody could approve it. That is the dangerous answer
arriving by omission, and omission is the common case: a tenant may publish its
own full version of a central ladder, and an editor changing one threshold does
not resend the fields it is not changing.

Existing rows are deliberately untouched. A default governs new stages only, and
the ladders already published carry values somebody chose - the seeded finance,
procurement and payout stages set False explicitly, and migration
``vs_procurement.0023`` already corrected the rows published before that. Rewriting
live stages here would change routing under documents mid-flight.

Reverse restores the old default, and is a true inverse: it changes no data.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0009_remove_unused_instance_submit_permission"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflowstage",
            name="skip_if_no_approvers",
            field=models.BooleanField(default=False),
        ),
    ]
