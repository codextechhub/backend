"""Give a platform audit event the branch of the document it is about.

Nullable: only finance and procurement events carry one, and events written
before this column existed are given theirs by ``manage.py branch_backfill``.
The table has no database trigger (append-only is enforced in
``AuditEvent.save``/``delete``), so nothing needs relaxing for that fill.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_audit", "0017_alter_auditevent_action_type"),
        ("vs_tenants", "0010_remove_branch__type"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditevent",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch of the document the event is about; null for the whole tenant.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="audit_events",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(
                fields=["tenant", "branch", "event_at"], name="vs_audit_branch_idx"
            ),
        ),
    ]
