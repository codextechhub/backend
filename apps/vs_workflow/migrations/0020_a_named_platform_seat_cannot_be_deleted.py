"""A CX chart seat named by an approval stage cannot be deleted.

The stage held the seat with ``SET_NULL``, so deleting the seat left a
SPECIFIC_POSITION stage naming nobody and every document reaching it parked.
``PROTECT`` refuses the delete instead, which the API answers 409, matching a
school's own posts and a seat an approver group names. Reversible: the reverse
restores ``SET_NULL``.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_user", "0015_retire_auth_event_log"),
        ("vs_workflow", "0019_a_named_post_on_a_tenants_own_chart"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflowstage",
            name="organogram_position",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="workflow_stages",
                to="vs_user.position",
            ),
        ),
    ]
