"""Index the two authorship paths used by personal decision history.

A normal decision belongs to ``actor``. A decision made while using a proxy
also belongs to the real person at the keyboard in ``proxied_by``. Both paths
sort newest first, so each index follows the same order as the history list.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0024_workflow_audit_append_only"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="workflowstageaction",
            index=models.Index(
                fields=["actor", "-acted_at"], name="wf_action_actor_time",
            ),
        ),
        migrations.AddIndex(
            model_name="workflowstageaction",
            index=models.Index(
                fields=["proxied_by", "-acted_at"], name="wf_action_proxy_time",
            ),
        ),
    ]
