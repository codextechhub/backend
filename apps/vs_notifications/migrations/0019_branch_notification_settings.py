"""Let a branch switch a notification channel on or off for itself.

Two columns and the constraints that keep them honest:

* ``NotificationSetting.branch`` - null is the tenant's row for all of its
  branches (and, with a null tenant, the platform default); set, it is one
  branch's own row. Uniqueness becomes one row per (tenant, branch, event type,
  channel), with the whole-tenant row still unique on its own.
* ``NotificationEventType.branch_scoped`` - whether the event is sent with the
  branch it is about, which is what makes a branch row for it mean anything.
  Set from the registry, the same list ``seed_notification_event_types``
  replays on every deploy.

Reversing drops every branch row before the old uniqueness returns, because a
database without the column can only read those rows as duplicate whole-tenant
rows. The branches' choices are lost on reverse; nothing that runs against the
old schema could honour them anyway.
"""
from django.db import migrations, models
from django.db.models import Q

# Imported rather than snapshotted, for the reasons 0008 gives: the registry is
# the one written-down catalogue, and replaying it installs today's flags.
from vs_notifications.constants import EVENT_TYPE_REGISTRY


def mark_branch_scoped_events(apps, schema_editor):
    """Copy ``branch_scoped`` from the registry onto the event types that exist."""
    NotificationEventType = apps.get_model("vs_notifications", "NotificationEventType")

    scoped = [entry["key"] for entry in EVENT_TYPE_REGISTRY if entry.get("branch_scoped")]
    NotificationEventType.objects.filter(key__in=scoped).update(branch_scoped=True)
    NotificationEventType.objects.exclude(key__in=scoped).update(branch_scoped=False)


def drop_branch_rows(apps, schema_editor):
    """Remove branch rows so the whole-tenant uniqueness can be restored."""
    NotificationSetting = apps.get_model("vs_notifications", "NotificationSetting")
    NotificationSetting.objects.filter(branch__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_notifications", "0018_in_app_notification_subjects"),
        ("vs_tenants", "0010_remove_branch__type"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationeventtype",
            name="branch_scoped",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when every place that sends this event says which branch it is "
                    "about, so a branch may switch its channels on or off for itself. "
                    "False keeps the event a whole-tenant setting: a branch row for it "
                    "cannot be written and would never be read."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="notificationeventtype",
            name="default_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Principled fallback when no NotificationSetting row (branch, tenant "
                    "or platform) exists for a (event_type, channel). Resolution order is: "
                    "branch row → tenant row → platform row → this value. Also the value "
                    "used to seed platform rows."
                ),
            ),
        ),
        migrations.AddField(
            model_name="notificationsetting",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="notification_settings",
                to="vs_tenants.branch",
                help_text=(
                    "Set only on a branch's own row, which must belong to the same tenant. "
                    "Null is the tenant's row for all of its branches. A branch that is "
                    "deleted takes its own choices with it: nothing else reads them."
                ),
            ),
        ),
        migrations.RemoveConstraint(
            model_name="notificationsetting",
            name="uq_notif_setting_tenant_scoped",
        ),
        # Forward: nothing to do. Reverse: runs before the old constraint returns.
        migrations.RunPython(migrations.RunPython.noop, drop_branch_rows),
        migrations.AddConstraint(
            model_name="notificationsetting",
            constraint=models.UniqueConstraint(
                condition=Q(tenant__isnull=False, branch__isnull=True),
                fields=("tenant", "event_type", "channel"),
                name="uq_notif_setting_tenant_scoped",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationsetting",
            constraint=models.UniqueConstraint(
                condition=Q(branch__isnull=False),
                fields=("tenant", "branch", "event_type", "channel"),
                name="uq_notif_setting_branch_scoped",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationsetting",
            constraint=models.CheckConstraint(
                condition=Q(branch__isnull=True) | Q(tenant__isnull=False),
                name="ck_notif_setting_branch_has_tenant",
            ),
        ),
        migrations.RunPython(mark_branch_scoped_events, migrations.RunPython.noop),
    ]
