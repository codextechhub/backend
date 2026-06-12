# =============================================================================
# vs_notifications / models.py
#
# Models:
#   NotificationEventType       — platform-defined event registry (seeded)
#   NotificationTemplate        — per-(event_type, channel) editable template
#   SchoolNotificationSetting   — per-school enable/disable toggle per channel
#   Notification                — dispatch record, one per recipient per channel
# =============================================================================

import uuid

from django.conf import settings
from django.db import models

from .constants import ChannelChoices, NotificationStatus
from vs_rbac.managers import TenantAwareManager


# ---------------------------------------------------------------------------
# 1.  NotificationEventType
# ---------------------------------------------------------------------------

class NotificationEventType(models.Model):
    """
    A named, platform-defined trigger point registered by a source module.

    Records are created and updated by the seed_notification_event_types
    management command.  They are never created via the API.

    The `key` field is the stable identifier used by calling modules:
        NotificationService.send(event_key="billing.invoice_issued", ...)

    Deleting an event type is blocked (PROTECT) while templates or settings
    reference it — use is_active=False to retire an event type instead.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    key = models.CharField(
        max_length=100,
        unique=True,
        help_text=(
            "Dot-notation identifier. e.g. billing.invoice_issued. "
            "Never changes after creation — other modules depend on this string."
        ),
    )
    label = models.CharField(
        max_length=200,
        help_text="Human-readable name displayed in School Admin notification settings.",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Explains when this event fires. Shown in the settings UI.",
    )
    source_module = models.CharField(
        max_length=100,
        help_text="The vs_* Django app that owns this event type. e.g. vs_billing.",
    )
    supported_channels = models.JSONField(
        default=list,
        help_text=(
            'List of channel strings this event supports. e.g. ["in_app", "email"]. '
            "Only channels listed here will be dispatched."
        ),
    )
    default_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Whether this event type is enabled by default when a new school is seeded. "
            "Does not retroactively affect existing school settings."
        ),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Platform-level kill switch. Inactive event types are never dispatched "
            "regardless of school settings. Use this to retire an event type."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_module", "key"]
        verbose_name = "Notification event type"
        verbose_name_plural = "Notification event types"

    def __str__(self):
        return f"{self.key} ({self.source_module})"

    def supports_channel(self, channel: str) -> bool:
        return channel in self.supported_channels


# ---------------------------------------------------------------------------
# 2.  NotificationTemplate
# ---------------------------------------------------------------------------

class NotificationTemplate(models.Model):
    """
    Stores the subject and body content for a specific (event_type, channel)
    pair.  One record exists per pair — enforced by unique_together.

    Vision Staff create and edit templates via the API or admin console.
    School users have no access to template management.

    Bodies support Django template syntax with {{ variable }} substitution.
    The available variables are defined per event type in constants.py and
    the FRD Section 8 Event Type Registry.

    Rendered content is stored on Notification records at dispatch time,
    so history remains stable even when the template is later updated.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    event_type = models.ForeignKey(
        NotificationEventType,
        on_delete=models.PROTECT,
        related_name="templates",
        help_text="The event type this template belongs to.",
    )
    channel = models.CharField(
        max_length=20,
        choices=ChannelChoices.CHOICES,
        help_text="The delivery channel this template renders for.",
    )
    subject = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Email subject line. Supports {{ variable }} substitution. "
            "Not used for in-app channel."
        ),
    )
    body = models.TextField(
        help_text=(
            "Notification body. Supports {{ variable }} substitution using "
            "Django template syntax. Stored rendered at dispatch time."
        ),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Inactive templates cause the channel to be silently skipped at "
            "dispatch time. Existing Notification records are unaffected."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_templates_created",
        help_text="Vision Staff user who created this template.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_templates_updated",
        help_text="Vision Staff user who last edited this template.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["event_type", "channel"]]
        ordering = ["event_type__source_module", "event_type__key", "channel"]
        verbose_name = "Notification template"
        verbose_name_plural = "Notification templates"

    def __str__(self):
        return f"{self.event_type.key} / {self.channel}"


# ---------------------------------------------------------------------------
# 3.  SchoolNotificationSetting
# ---------------------------------------------------------------------------

class SchoolNotificationSetting(models.Model):
    """
    Per-school, per-event-type, per-channel configuration.

    Controls whether the dispatch service creates Notification records for
    a given school on a given channel.

    Records are created by seed_notification_settings management command
    (called after school provisioning in vs_onboarding).  School Admins
    update is_enabled on existing records via PATCH.  They do not create
    or delete records.

    The IN_APP channel cannot be disabled — enforced at the serializer and
    service layer.  Attempting to set is_enabled=False for IN_APP raises
    InAppAlwaysEnabledError.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    school = models.ForeignKey(
        "vs_schools.School",
        on_delete=models.CASCADE,
        related_name="notification_settings",
        help_text="The school these settings apply to.",
    )
    event_type = models.ForeignKey(
        NotificationEventType,
        on_delete=models.CASCADE,
        related_name="school_settings",
        help_text="The event type being configured.",
    )
    channel = models.CharField(
        max_length=20,
        choices=ChannelChoices.CHOICES,
        help_text="The channel being configured.",
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Whether this (event_type, channel) fires for this school. "
            "School Admins toggle this. IN_APP cannot be set to False."
        ),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_settings_updated",
        help_text="The admin who last changed this setting.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        unique_together = [["school", "event_type", "channel"]]
        indexes = [
            models.Index(fields=["school", "event_type"]),
            models.Index(fields=["school", "channel", "is_enabled"]),
        ]
        verbose_name = "School notification setting"
        verbose_name_plural = "School notification settings"

    def __str__(self):
        status = "on" if self.is_enabled else "off"
        return f"{self.school_id} / {self.event_type.key} / {self.channel} ({status})"


# ---------------------------------------------------------------------------
# 4.  Notification
# ---------------------------------------------------------------------------

class Notification(models.Model):
    """
    The central dispatch record.  One Notification is created per recipient
    per channel per dispatch event.

    In-app notifications:
        Created with status=SENT and dispatched_at=now().  No Celery task
        needed — database write IS delivery.

    Email notifications:
        Created with status=PENDING.  A Celery task (deliver_email_notification)
        is enqueued via transaction.on_commit to avoid firing on rollback.
        The task transitions status to SENT or FAILED.

    Rendered content (subject, body) is stored at dispatch time from the
    NotificationTemplate.  History remains accurate even if the template
    is later edited or deactivated.

    The recipient FK allows NULL to support the user.invited event type,
    where the recipient has no User account yet.  In that case,
    unregistered_email stores the target address.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    school = models.ForeignKey(
        "vs_schools.School",
        on_delete=models.PROTECT,
        related_name="notifications",
        null=True,
        blank=True,
        help_text=(
            "School of the recipient — the tenant scoping anchor. "
            "Null only for platform-level notifications to CX staff "
            "(e.g. background-task completion alerts)."
        ),
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="notifications",
        help_text=(
            "The target User. Null only for unregistered recipients "
            "(e.g. user.invited before the account is created)."
        ),
    )
    unregistered_email = models.EmailField(
        blank=True,
        default="",
        help_text=(
            "Used only when recipient is null (unregistered recipient path). "
            "Stores the email address for the Celery task to dispatch to."
        ),
    )
    event_type = models.ForeignKey(
        NotificationEventType,
        on_delete=models.PROTECT,
        related_name="notifications",
        help_text="The event type that triggered this notification.",
    )
    channel = models.CharField(
        max_length=20,
        choices=ChannelChoices.CHOICES,
        help_text="Which delivery channel this record represents.",
    )
    subject = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Rendered subject line (email only). Stored at dispatch time.",
    )
    body = models.TextField(
        help_text="Rendered body after variable substitution. Stored at dispatch time.",
    )
    status = models.CharField(
        max_length=20,
        choices=NotificationStatus.CHOICES,
        default=NotificationStatus.PENDING,
        help_text=(
            "Delivery status. "
            "IN_APP: always SENT on creation. "
            "EMAIL: PENDING → SENT or FAILED via Celery task."
        ),
    )
    failure_reason = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Populated on FAILED. Stores the last Celery task exception message "
            "or a sentinel value (e.g. NO_EMAIL_ADDRESS) for pre-flight failures."
        ),
    )
    retry_count = models.IntegerField(
        default=0,
        help_text="Number of dispatch attempts made by the Celery task.",
    )
    is_read = models.BooleanField(
        default=False,
        help_text="In-app channel only. True once the recipient marks the notification as read.",
    )
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when is_read was set to True.",
    )
    dispatched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the first successful dispatch.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["-created_at"]
        indexes = [
            # Primary feed query: user's unread in-app notifications
            models.Index(fields=["recipient", "channel", "is_read", "-created_at"]),
            # Admin history log queries
            models.Index(fields=["school", "event_type", "status"]),
            models.Index(fields=["school", "channel", "status", "-created_at"]),
            # Celery task lookup (status=PENDING email notifications)
            models.Index(fields=["status", "channel", "-created_at"]),
        ]
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"

    def __str__(self):
        recipient_label = (
            self.recipient.email if self.recipient_id else self.unregistered_email
        )
        return f"{self.event_type.key} → {recipient_label} [{self.channel}] {self.status}"

    @property
    def effective_email(self) -> str:
        """
        Returns the email address to dispatch to, regardless of whether
        the recipient is a registered User or an unregistered invite target.
        """
        if self.recipient_id and self.recipient.email:
            return self.recipient.email
        return self.unregistered_email
