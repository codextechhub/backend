# =============================================================================
# vs_notifications / services / dispatch.py
#
# NotificationService — the primary entry point for all notification dispatch.
#
# Called by other module services (vs_finance, vs_workflow, vs_user, etc.).
# Never called directly from views.
#
# Notifications are RECIPIENT-centric. `school` is an optional scope stored on
# each record for filtering/history; it is NOT required to dispatch. CX staff
# and any other school-less recipients are first-class.
#
# Responsibilities:
#   - Validate the event key
#   - Resolve which channels fire (resolve_channels — school row → platform row
#     → default_enabled; transactional events bypass settings)
#   - Render templates (subject, plain body, optional HTML body)
#   - Create Notification records (storing metadata + html_body)
#   - Enqueue Celery tasks via transaction.on_commit (email only)
#   - Fire notification_failed for pre-flight FAILED email records after commit
# =============================================================================

import logging
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from ..constants import ChannelChoices, NotificationStatus
from ..exceptions import UnknownEventTypeError, TemplateRenderError
from ..models import Notification, NotificationEventType, NotificationTemplate
from ..signals import notification_failed
from .render import render_notification_template
from .settings import resolve_channels

logger = logging.getLogger("vs_notifications.dispatch")


# ---------------------------------------------------------------------------
# Unregistered recipient dataclass
# Used for the user.invited path where no User record exists yet.
# ---------------------------------------------------------------------------

# Represent email-only recipients before they have a User row.
@dataclass
class UnregisteredRecipient:
    """
    Represents an email recipient who does not yet have a User account.
    Used for events like user.invited / user.password_reset.
    """
    email: str
    name: str = ""


# ---------------------------------------------------------------------------
# NotificationService
# ---------------------------------------------------------------------------

class NotificationService:
    """
    Primary dispatch service for vs_notifications.

    Usage example (from vs_finance):

        from vs_notifications.services.dispatch import NotificationService

        NotificationService.send(
            event_key="billing.invoice_overdue",
            context={...},
            recipients=[guardian_user],
            school=school,            # optional — omit for school-less recipients
        )
    """

    # Orchestrate template rendering, record creation, and post-commit delivery.
    @staticmethod
    def send(
        event_key: str,
        context: dict,
        recipients: list,
        school=None,
        suppress: bool = False,
        unregistered_recipients: Optional[list[UnregisteredRecipient]] = None,
        metadata: Optional[dict] = None,
    ) -> list:
        """
        Dispatch notifications for a given event to a list of recipients.

        Args:
            event_key:               Dot-notation event key, e.g. "user.invited".
            context:                 Dict of template variables.
            recipients:              List of User instances to notify.
            school:                  Optional School instance. Stored on each
                                     record for filtering/history and used to
                                     resolve school-specific settings overrides.
                                     Defaults to None (platform scope).
            suppress:                If True, return immediately without dispatching.
            unregistered_recipients: Optional list of UnregisteredRecipient — for
                                     recipients who have no User account yet.
            metadata:                Optional dict stored on EVERY created record's
                                     internal-only metadata field (e.g. an
                                     activation_key for delivery-signal receivers).
                                     Never exposed via any serializer.

        Returns:
            List of created Notification UUIDs (as strings).
            Empty list if suppress=True or no channels are enabled.

        Raises:
            UnknownEventTypeError: If event_key does not match an active event type.
        """
        if suppress:
            logger.debug("Notification suppressed for event_key=%s", event_key)
            return []

        metadata = metadata or {}

        # ── 1. Resolve event type ──────────────────────────────────────────
        try:
            event_type = NotificationEventType.objects.get(
                key=event_key,
                is_active=True,
            )
        except NotificationEventType.DoesNotExist:
            raise UnknownEventTypeError(
                message=f"Unknown or inactive notification event key: '{event_key}'",
            )

        # ── 2. Resolve which channels fire (school row → platform → default;
        #        transactional events bypass settings). ──────────────────────
        channel_enabled = resolve_channels(event_type, school=school)

        # ── 3. Pre-fetch templates for enabled channels ────────────────────
        enabled_channels = [ch for ch, on in channel_enabled.items() if on]
        if not enabled_channels:
            logger.debug(
                "All channels disabled for event_key=%s (school=%s) — nothing to dispatch.",
                event_key,
                getattr(school, "id", None),
            )
            return []

        templates = _fetch_templates(event_type, enabled_channels)

        # ── 4. Build Notification records ─────────────────────────────────
        notifications_to_create = []

        all_targets = _build_targets(recipients, unregistered_recipients or [])

        for target in all_targets:
            # Each target gets one record per enabled channel.
            for channel in enabled_channels:
                template = templates.get(channel)
                if template is None:
                    logger.warning(
                        "No active template for event_key=%s channel=%s — channel skipped.",
                        event_key,
                        channel,
                    )
                    continue

                # Email delivery cannot proceed without an address, but history should record the failure.
                email_addr = _resolve_email(target)
                if channel == ChannelChoices.EMAIL and not email_addr:
                    # Pre-flight FAILED — no point rendering or queuing.
                    notifications_to_create.append(
                        _build_failed_notification(
                            event_type=event_type,
                            channel=channel,
                            school=school,
                            target=target,
                            failure_reason="NO_EMAIL_ADDRESS",
                            metadata=metadata,
                        )
                    )
                    continue

                # Render template (subject, plain body, optional HTML body).
                try:
                    rendered_subject, rendered_body, rendered_html = (
                        render_notification_template(template, context)
                    )
                except TemplateRenderError as exc:
                    logger.error(
                        "Template render failed for event_key=%s channel=%s: %s",
                        event_key, channel, exc,
                    )
                    notifications_to_create.append(
                        _build_failed_notification(
                            event_type=event_type,
                            channel=channel,
                            school=school,
                            target=target,
                            failure_reason=str(exc),
                            metadata=metadata,
                        )
                    )
                    continue

                # In-app notifications are complete once stored; email waits for the delivery task.
                is_in_app = channel == ChannelChoices.IN_APP
                notifications_to_create.append(
                    _build_notification(
                        event_type=event_type,
                        channel=channel,
                        school=school,
                        target=target,
                        subject=rendered_subject,
                        body=rendered_body,
                        html_body=rendered_html if not is_in_app else "",
                        metadata=metadata,
                        # IN_APP is immediately SENT — no async task needed
                        status=NotificationStatus.SENT if is_in_app else NotificationStatus.PENDING,
                        dispatched_at=timezone.now() if is_in_app else None,
                    )
                )

        if not notifications_to_create:
            return []

        # ── 5. Bulk-create all records atomically ──────────────────────────
        created = Notification.objects.bulk_create(notifications_to_create)
        created_ids = [str(n.id) for n in created]

        # ── 6. Post-commit side effects ────────────────────────────────────
        # Email PENDING → enqueue delivery. Email FAILED (pre-flight) → fire the
        # notification_failed signal so downstream trackers see the terminal
        # state even though no delivery task runs. Both wait for commit so a
        # rollback never enqueues or signals a phantom record.
        email_ids = [
            str(n.id)
            for n in created
            if n.channel == ChannelChoices.EMAIL
            and n.status == NotificationStatus.PENDING
        ]
        preflight_failed = [
            n
            for n in created
            if n.channel == ChannelChoices.EMAIL
            and n.status == NotificationStatus.FAILED
        ]

        if email_ids or preflight_failed:
            def _after_commit():
                if email_ids:
                    from ..tasks import deliver_email_notification
                    for notif_id in email_ids:
                        deliver_email_notification.delay(notif_id)
                for notif in preflight_failed:
                    # Pre-flight failures have no task, so emit the same terminal signal here.
                    notification_failed.send(
                        sender=Notification, notification=notif
                    )

            transaction.on_commit(_after_commit)

        logger.info(
            "Dispatched %d notification records for event_key=%s "
            "(email tasks: %d, pre-flight failed: %d).",
            len(created_ids),
            event_key,
            len(email_ids),
            len(preflight_failed),
        )
        return created_ids


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# Fetch active templates for the channels that actually need dispatch.
def _fetch_templates(event_type, channels: list) -> dict:
    """
    Return a dict of {channel: NotificationTemplate} for the given channels.
    Only active templates are returned.  Missing or inactive templates produce
    no entry in the dict — callers handle the None case.
    """
    qs = NotificationTemplate.objects.filter(
        event_type=event_type,
        channel__in=channels,
        is_active=True,
    )
    return {t.channel: t for t in qs}


# Merge registered and email-only recipients into one dispatch target list.
def _build_targets(recipients: list, unregistered: list) -> list:
    """
    Combine registered User instances and UnregisteredRecipient dataclasses
    into a single iterable for the dispatch loop.
    """
    return list(recipients) + list(unregistered)


# Read an email address from either a User-like object or an invite target.
def _resolve_email(target) -> str:
    """
    Extract the email address from a target, regardless of whether it is a
    registered User or an UnregisteredRecipient.
    """
    if isinstance(target, UnregisteredRecipient):
        return target.email
    return getattr(target, "email", "") or ""


# Build the unsaved record for a successful in-app notification or queued email.
def _build_notification(
    event_type, channel, school, target, subject, body, html_body,
    metadata, status, dispatched_at,
) -> Notification:
    """
    Construct an unsaved Notification instance.
    Handles both registered Users and UnregisteredRecipient targets.
    """
    is_unregistered = isinstance(target, UnregisteredRecipient)
    return Notification(
        event_type=event_type,
        channel=channel,
        school=school,
        recipient=None if is_unregistered else target,
        unregistered_email=target.email if is_unregistered else "",
        subject=subject,
        body=body,
        html_body=html_body,
        metadata=metadata,
        status=status,
        dispatched_at=dispatched_at,
    )


# Build the unsaved record for failures detected before Celery delivery.
def _build_failed_notification(
    event_type, channel, school, target, failure_reason: str, metadata: dict,
) -> Notification:
    """
    Construct an unsaved Notification instance pre-set to FAILED.
    Used for pre-flight failures (no email address, render error)
    where no Celery task should be enqueued.
    """
    is_unregistered = isinstance(target, UnregisteredRecipient)
    return Notification(
        event_type=event_type,
        channel=channel,
        school=school,
        recipient=None if is_unregistered else target,
        unregistered_email=target.email if is_unregistered else "",
        subject="",
        body="",
        html_body="",
        metadata=metadata,
        status=NotificationStatus.FAILED,
        failure_reason=failure_reason,
    )
