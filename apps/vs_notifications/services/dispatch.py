# =============================================================================
# vs_notifications / services / dispatch.py
#
# NotificationService — the primary entry point for all notification dispatch.
#
# Called by other module services (vs_billing, vs_workflow, vs_students, etc.).
# Never called directly from views.
#
# Responsibilities:
#   - Validate the event key
#   - Resolve channel settings for the school
#   - Render templates
#   - Create Notification records
#   - Enqueue Celery tasks via transaction.on_commit (email only)
# =============================================================================

import logging
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from ..constants import ChannelChoices, NotificationStatus
from ..exceptions import UnknownEventTypeError, TemplateRenderError
from ..models import Notification, NotificationEventType, NotificationTemplate
from .render import render_notification_template
from .settings import bulk_is_channel_enabled

logger = logging.getLogger("vs_notifications.dispatch")


# ---------------------------------------------------------------------------
# Unregistered recipient dataclass
# Used for the user.invited path where no User record exists yet.
# ---------------------------------------------------------------------------

@dataclass
class UnregisteredRecipient:
    """
    Represents an email recipient who does not yet have a User account.
    Used exclusively for the user.invited event type.
    """
    email: str
    name: str = ""


# ---------------------------------------------------------------------------
# NotificationService
# ---------------------------------------------------------------------------

class NotificationService:
    """
    Primary dispatch service for vs_notifications.

    Usage example (from vs_billing):

        from vs_notifications.services.dispatch import NotificationService

        NotificationService.send(
            event_key="billing.invoice_issued",
            context={
                "student_first_name": student.first_name,
                "student_last_name":  student.last_name,
                "invoice_number":     invoice.number,
                "invoice_amount":     str(invoice.total_amount),
                "due_date":           invoice.due_date.strftime("%d %b %Y"),
                "school_name":        school.name,
                "payment_link":       payment_url,
            },
            recipients=[guardian_user],
            school=school,
        )
    """

    @staticmethod
    def send(
        event_key: str,
        context: dict,
        recipients: list,
        school,
        suppress: bool = False,
        unregistered_recipients: Optional[list[UnregisteredRecipient]] = None,
    ) -> list:
        """
        Dispatch notifications for a given event to a list of recipients.

        Args:
            event_key:               Dot-notation event key, e.g. "billing.invoice_issued".
            context:                 Dict of template variables. All keys defined in the
                                     FRD Section 8 registry for this event must be present.
            recipients:              List of User instances to notify.
            school:                  The School instance (tenant scope).
            suppress:                If True, return immediately without dispatching.
                                     Use for bulk operations where notification is noise.
            unregistered_recipients: Optional list of UnregisteredRecipient dataclass
                                     instances.  Used exclusively for user.invited —
                                     recipients who have no User account yet.

        Returns:
            List of created Notification UUIDs (as strings).
            Empty list if suppress=True or no channels are enabled.

        Raises:
            UnknownEventTypeError: If event_key does not match an active event type.
        """
        if suppress:
            logger.debug("Notification suppressed for event_key=%s", event_key)
            return []

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

        # ── 2. Resolve channel settings for this school ────────────────────
        channel_enabled = bulk_is_channel_enabled(school, event_type)

        # ── 3. Pre-fetch templates for enabled channels ────────────────────
        enabled_channels = [ch for ch, on in channel_enabled.items() if on]
        if not enabled_channels:
            logger.debug(
                "All channels disabled for school=%s event_key=%s — nothing to dispatch.",
                getattr(school, "slug", school.id),
                event_key,
            )
            return []

        templates = _fetch_templates(event_type, enabled_channels)

        # ── 4. Build Notification records ─────────────────────────────────
        notifications_to_create = []

        all_targets = _build_targets(recipients, unregistered_recipients or [])

        for target in all_targets:
            for channel in enabled_channels:
                template = templates.get(channel)
                if template is None:
                    logger.warning(
                        "No active template for event_key=%s channel=%s — channel skipped.",
                        event_key,
                        channel,
                    )
                    continue

                # Check email address availability before rendering
                email_addr = _resolve_email(target)
                if channel == ChannelChoices.EMAIL and not email_addr:
                    # Create a FAILED record immediately — no point rendering or queuing
                    notifications_to_create.append(
                        _build_failed_notification(
                            event_type=event_type,
                            channel=channel,
                            school=school,
                            target=target,
                            failure_reason="NO_EMAIL_ADDRESS",
                        )
                    )
                    continue

                # Render template
                try:
                    rendered_subject, rendered_body = render_notification_template(
                        template, context
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
                        )
                    )
                    continue

                # Build the record
                is_in_app = channel == ChannelChoices.IN_APP
                notifications_to_create.append(
                    _build_notification(
                        event_type=event_type,
                        channel=channel,
                        school=school,
                        target=target,
                        subject=rendered_subject,
                        body=rendered_body,
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

        # ── 6. Enqueue Celery tasks for email records after commit ─────────
        email_ids = [
            str(n.id)
            for n in created
            if n.channel == ChannelChoices.EMAIL
            and n.status == NotificationStatus.PENDING
        ]

        if email_ids:
            def enqueue_email_tasks():
                from ..tasks import deliver_email_notification
                for notif_id in email_ids:
                    deliver_email_notification.delay(notif_id)

            transaction.on_commit(enqueue_email_tasks)

        logger.info(
            "Dispatched %d notification records for event_key=%s (email tasks: %d).",
            len(created_ids),
            event_key,
            len(email_ids),
        )
        return created_ids


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

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


def _build_targets(recipients: list, unregistered: list) -> list:
    """
    Combine registered User instances and UnregisteredRecipient dataclasses
    into a single iterable for the dispatch loop.
    """
    # Wrap User instances in a simple container so the loop uses one code path
    return list(recipients) + list(unregistered)


def _resolve_email(target) -> str:
    """
    Extract the email address from a target, regardless of whether it is a
    registered User or an UnregisteredRecipient.
    """
    if isinstance(target, UnregisteredRecipient):
        return target.email
    return getattr(target, "email", "") or ""


def _build_notification(
    event_type, channel, school, target, subject, body, status, dispatched_at
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
        status=status,
        dispatched_at=dispatched_at,
    )


def _build_failed_notification(
    event_type, channel, school, target, failure_reason: str
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
        status=NotificationStatus.FAILED,
        failure_reason=failure_reason,
    )
