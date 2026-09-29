"""Record who really acted when a workflow step was taken under a proxy.

A vote cast under a proxy (an impersonation session) names the impersonated
person as its actor, which is right for eligibility and quorum but hides the
person at the keyboard. ``WorkflowStageAction.proxied_by`` holds that person.
Audit rows already name the real person as actor; ``WorkflowAuditLog.
effective_user`` holds the impersonated one, which until now lived only in the
row's ``context``.

Both are filled for existing rows from what the audit log already knows: a
proxied audit row carries ``effective_user_id`` and ``impersonation_session_id``
in its context, and the APPROVER_ACTED row for a vote carries the vote's id.
Only empty columns are written, so the backfill is safe to run twice. The
reverse leaves the data alone and lets the column drop take it.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def fill_proxy_attribution(apps, schema_editor):
    AuditLog = apps.get_model("vs_workflow", "WorkflowAuditLog")
    Action = apps.get_model("vs_workflow", "WorkflowStageAction")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    proxied = AuditLog.objects.filter(
        context__has_key="impersonation_session_id",
        effective_user__isnull=True,
    )
    user_ids = {
        uid for uid in proxied.values_list("context__effective_user_id", flat=True)
        if uid is not None
    }
    existing = set(User.objects.filter(pk__in=user_ids).values_list("pk", flat=True))
    for uid in existing:
        proxied.filter(context__effective_user_id=uid).update(effective_user_id=uid)

    votes = AuditLog.objects.filter(
        event_type="APPROVER_ACTED",
        context__has_keys=["impersonation_session_id", "action_id"],
        actor__isnull=False,
    )
    for action_id, actor_id in votes.values_list("context__action_id", "actor_id"):
        Action.objects.filter(pk=action_id, proxied_by__isnull=True).update(
            proxied_by_id=actor_id,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0020_a_named_platform_seat_cannot_be_deleted"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowauditlog",
            name="effective_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowstageaction",
            name="proxied_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(fill_proxy_attribution, migrations.RunPython.noop),
    ]
