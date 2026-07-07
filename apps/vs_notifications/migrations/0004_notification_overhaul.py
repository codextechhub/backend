# =============================================================================
# vs_notifications / migrations / 0004_notification_overhaul.py
#
# Recipient-centric notification overhaul:
#   * Rename SchoolNotificationSetting → NotificationSetting (data survives).
#   * school FK becomes nullable (NULL = platform-wide default row).
#   * Swap unique_together for two conditional UniqueConstraints (Postgres
#     partial indexes; Django abstracts these on SQLite too).
#   * Add NotificationEventType.is_transactional, NotificationTemplate.html_body,
#     Notification.html_body + Notification.metadata.
#   * Seed platform (school=NULL) setting rows from each event type's
#     default_enabled (data migration; reversible — the reverse deletes only the
#     NULL-school rows it would create).
# =============================================================================

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def seed_platform_settings(apps, schema_editor):
    """
    For every active, non-transactional event type × supported channel, create a
    platform-wide NotificationSetting (school=NULL) with is_enabled taken from
    the event type's default_enabled — if one does not already exist.
    """
    NotificationEventType = apps.get_model("vs_notifications", "NotificationEventType")
    NotificationSetting = apps.get_model("vs_notifications", "NotificationSetting")

    for et in NotificationEventType.objects.filter(is_active=True, is_transactional=False):
        for channel in et.supported_channels:
            NotificationSetting.objects.get_or_create(
                school=None,
                event_type=et,
                channel=channel,
                defaults={"is_enabled": et.default_enabled},
            )


def unseed_platform_settings(apps, schema_editor):
    """Reverse: delete the platform-wide (school=NULL) rows this migration seeds."""
    NotificationSetting = apps.get_model("vs_notifications", "NotificationSetting")
    NotificationSetting.objects.filter(school__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_notifications", "0003_alter_notification_school"),
        ("vs_schools", "0001_initial"),
    ]

    operations = [
        # ── 1. Rename the settings model (preserves the table + its data) ────
        migrations.RenameModel(
            old_name="SchoolNotificationSetting",
            new_name="NotificationSetting",
        ),

        # ── 2. New fields ────────────────────────────────────────────────────
        migrations.AddField(
            model_name="notificationeventtype",
            name="is_transactional",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Transactional events (e.g. password resets, invitations) "
                    "bypass NotificationSetting checks entirely — they always "
                    "dispatch on their supported channels. The platform kill "
                    "switch (is_active) still wins over everything."
                ),
            ),
        ),
        migrations.AddField(
            model_name="notificationtemplate",
            name="html_body",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Optional HTML body for the email channel. Supports "
                    "{{ variable }} substitution. When present, email delivery "
                    "becomes multipart (plain-text body + HTML alternative). "
                    "Ignored for in-app."
                ),
            ),
        ),
        migrations.AddField(
            model_name="notification",
            name="html_body",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Rendered HTML body (email only). Populated at dispatch time "
                    "when the template defines an html_body. When present, "
                    "delivery is multipart."
                ),
            ),
        ),
        migrations.AddField(
            model_name="notification",
            name="metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Internal-only caller correlation data (e.g. activation_key "
                    "for invitation tracking). NEVER exposed in any serializer (FLS)."
                ),
            ),
        ),

        # ── 3. Field alterations ────────────────────────────────────────────
        migrations.AlterField(
            model_name="notificationeventtype",
            name="default_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Principled fallback when no NotificationSetting row (school "
                    "or platform) exists for a (event_type, channel). Resolution "
                    "order is: school row → platform row → this value. Also the "
                    "value used to seed platform rows."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="body",
            field=models.TextField(
                help_text=(
                    "Notification body (plain text). Supports {{ variable }} "
                    "substitution using Django template syntax. Stored rendered "
                    "at dispatch time."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="notificationsetting",
            name="is_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Whether this (event_type, channel) fires for this scope. "
                    "Admins toggle this. IN_APP cannot be set to False."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="body",
            field=models.TextField(
                help_text=(
                    "Rendered plain-text body after substitution. Stored at "
                    "dispatch time."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="school",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Optional scope for filtering/history. NOT a dispatch "
                    "requirement — notifications are recipient-centric. Null for "
                    "platform-level recipients (CX staff, and any recipient with "
                    "no school)."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="notifications",
                to="vs_schools.school",
            ),
        ),
        migrations.AlterField(
            model_name="notificationsetting",
            name="school",
            field=models.ForeignKey(
                blank=True,
                null=True,
                help_text=(
                    "The school this override applies to. NULL means a "
                    "platform-wide default that applies to every recipient "
                    "without a school override."
                ),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notification_settings",
                to="vs_schools.school",
            ),
        ),
        migrations.AlterField(
            model_name="notificationsetting",
            name="event_type",
            field=models.ForeignKey(
                help_text="The event type being configured.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="settings",
                to="vs_notifications.notificationeventtype",
            ),
        ),

        # ── 4. Constraints + indexes on NotificationSetting ─────────────────
        migrations.AlterUniqueTogether(
            name="notificationsetting",
            unique_together=set(),
        ),
        # Drop the two old (school, ...) indexes created in 0002 for the old model.
        migrations.RemoveIndex(
            model_name="notificationsetting",
            name="vs_notifica_school__f6014f_idx",
        ),
        migrations.RemoveIndex(
            model_name="notificationsetting",
            name="vs_notifica_school__70317f_idx",
        ),
        migrations.AddConstraint(
            model_name="notificationsetting",
            constraint=models.UniqueConstraint(
                fields=("school", "event_type", "channel"),
                condition=Q(school__isnull=False),
                name="uq_notif_setting_school_scoped",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationsetting",
            constraint=models.UniqueConstraint(
                fields=("event_type", "channel"),
                condition=Q(school__isnull=True),
                name="uq_notif_setting_platform",
            ),
        ),
        migrations.AddIndex(
            model_name="notificationsetting",
            index=models.Index(
                fields=["event_type", "channel", "school"],
                name="vs_notifica_event_t_c986ff_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="notificationsetting",
            index=models.Index(
                fields=["school", "channel", "is_enabled"],
                name="vs_notifica_school__f46d44_idx",
            ),
        ),

        # ── 5. Model options (verbose names) ────────────────────────────────
        migrations.AlterModelOptions(
            name="notificationsetting",
            options={
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "verbose_name": "Notification setting",
                "verbose_name_plural": "Notification settings",
            },
        ),

        # ── 6. Seed platform default rows (reversible) ──────────────────────
        migrations.RunPython(seed_platform_settings, unseed_platform_settings),
    ]
