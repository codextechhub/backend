# =============================================================================
# vs_notifications / models.py
#
# Models:
#   NotificationEventType   — platform-defined event registry (seeded)
#   NotificationTemplate    — per-(event_type, channel) editable template
#   NotificationSetting     — enable/disable toggle per channel; school-scoped
#                             OR platform-wide (school=NULL)
#   Notification            — dispatch record, one per recipient per channel
#
# The platform is global: school users are only a fraction of notification
# consumers (CX staff and future user types have no school). Notifications are
# therefore RECIPIENT-centric — `Notification.school` is a nullable filter/
# history anchor, not a dispatch requirement. Settings layer the same way:
# a NotificationSetting with school=NULL is a platform-wide default.
# =============================================================================

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

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
            "Principled fallback when no NotificationSetting row (school or "
            "platform) exists for a (event_type, channel). Resolution order is: "
            "school row → platform row → this value. Also the value used to seed "
            "platform rows."
        ),
    )
    is_transactional = models.BooleanField(
        default=False,
        help_text=(
            "Transactional events (e.g. password resets, invitations) bypass "
            "NotificationSetting checks entirely — they always dispatch on their "
            "supported channels. The platform kill switch (is_active) still wins "
            "over everything."
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
            "Notification body (plain text). Supports {{ variable }} substitution "
            "using Django template syntax. Stored rendered at dispatch time."
        ),
    )
    html_body = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Optional HTML body for the email channel. Supports {{ variable }} "
            "substitution. When present, email delivery becomes multipart "
            "(plain-text body + HTML alternative). Ignored for in-app."
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
# 3.  NotificationSetting
# ---------------------------------------------------------------------------

class NotificationSetting(models.Model):
    """
    Per-event-type, per-channel enable/disable toggle.

    Two flavours, distinguished by `school`:
      * school-scoped  (school=<School>)  — a school's override.
      * platform-wide  (school=NULL)      — the default for every recipient
                                            that has no school-specific override.

    Resolution (most specific wins, see services/settings.resolve_channels):
        school row → platform row → event_type.default_enabled.

    Platform rows are seeded from each event type's default_enabled (by the
    data migration and seed command). School rows are written by School Admins
    via PATCH; CX staff write platform rows (or a specific school's rows).

    The IN_APP channel cannot be disabled — enforced where settings are WRITTEN
    (serializer/service). resolve_channels only reads rows; it does not silently
    override a persisted value.

    The default manager is TenantAware with include_global=True: a school-scoped
    request sees its own rows PLUS the platform (school=NULL) rows. `all_objects`
    is the unscoped escape hatch used by the service layer (Celery has no
    thread-local tenant context) and by explicit view scoping.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="notification_settings", null=True, blank=True,
        help_text="Null only for platform-wide defaults.",
    )
    event_type = models.ForeignKey(
        NotificationEventType,
        on_delete=models.CASCADE,
        related_name="settings",
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
            "Whether this (event_type, channel) fires for this scope. "
            "Admins toggle this. IN_APP cannot be set to False."
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

    objects = TenantAwareManager(include_global=True)
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            # A school may hold at most one row per (event_type, channel).
            models.UniqueConstraint(
                fields=["tenant", "event_type", "channel"],
                condition=Q(tenant__isnull=False),
                name="uq_notif_setting_tenant_scoped",
            ),
            # At most one platform-wide row per (event_type, channel).
            models.UniqueConstraint(
                fields=["event_type", "channel"],
                condition=Q(tenant__isnull=True),
                name="uq_notif_setting_platform",
            ),
        ]
        indexes = [
            # resolve_channels fetches both the school row and the platform row
            # (school IN (<id>, NULL)) for one (event_type) in a single query.
            models.Index(fields=["event_type", "channel", "tenant"]),
            models.Index(fields=["tenant", "channel", "is_enabled"]),
        ]
        verbose_name = "Notification setting"
        verbose_name_plural = "Notification settings"

    def __str__(self):
        status = "on" if self.is_enabled else "off"
        scope = self.tenant_id if self.tenant_id else "platform"
        return f"{scope} / {self.event_type.key} / {self.channel} ({status})"


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
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="notifications",
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
        help_text="Rendered plain-text body after substitution. Stored at dispatch time.",
    )
    html_body = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Rendered HTML body (email only). Populated at dispatch time when the "
            "template defines an html_body. When present, delivery is multipart."
        ),
    )
    # Internal-only correlation store, never serialized (FLS). Recognised keys:
    #   activation_key — invitation tracking correlation for the delivery-signal
    #                    receivers (vs_user.receivers).
    #   from_name      — per-message From display name; deliver_email_notification
    #                    builds the From address from it via build_from_email.
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Internal-only caller correlation data (e.g. activation_key for "
            "invitation tracking). NEVER exposed in any serializer (FLS)."
        ),
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
            models.Index(fields=["tenant", "event_type", "status"]),
            models.Index(fields=["tenant", "channel", "status", "-created_at"]),
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
