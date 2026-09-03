"""NotificationService - the primary entry point for all notification dispatch.

Called by other module services (vs_finance, vs_workflow, vs_user, etc.).
Never called directly from views.

Notifications are RECIPIENT-centric, and so is ownership: each record is
stamped with the RECIPIENT'S OWN tenant, because that is the tenant whose
inbox and whose history log the record shows up in. The tenant/school a
caller passes says what the message is ABOUT and is stored separately as
`origin_tenant`. The two are the same party for most events, and diverge the
moment one tenant's activity is reported to another's staff (a school's
support ticket going to platform triage). Stamping the caller's tenant on
every row put internal support notes inside the school's own history log.

A single send may therefore span tenants: recipients are grouped by owner
tenant and each group resolves its own channel settings, so a school muting
an event cannot silence the platform staff reading the same event. CX staff
and any other school-less recipients are first-class.

Responsibilities:
  - Validate the event key
  - Resolve which channels fire (resolve_channels - school row → platform row
    → default_enabled; transactional events bypass settings)
  - Render templates (subject, plain body, optional HTML body)
  - Create Notification records (storing metadata + html_body)
  - Enqueue Celery tasks via transaction.on_commit (email only)
  - Fire notification_failed for pre-flight FAILED email records after commit
"""
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
            school=school,   # what the message is ABOUT; recipients still
                             # own their own records
        )
    """

    # Orchestrate template rendering, record creation, and post-commit delivery.
    @staticmethod
    def send(
        event_key: str,
        context: dict,
        recipients: list,
        tenant=None,
        school=None,
        suppress: bool = False,
        unregistered_recipients: Optional[list[UnregisteredRecipient]] = None,
        metadata: Optional[dict] = None,
        delivery_replacements: Optional[dict[str, str]] = None,
    ) -> list:
        """
        Dispatch notifications for a given event to a list of recipients.

        Args:
            event_key:               Dot-notation event key, e.g. "user.invited".
            context:                 Dict of template variables.
            recipients:              List of User instances to notify. Each
                                     recipient's own tenant owns their records.
            tenant:                  Optional Tenant the event is ABOUT. Stored
                                     as origin_tenant, and used as the owner
                                     only for recipients with no tenant of their
                                     own (unregistered addresses).
            school:                  Optional School instance, read for its
                                     tenant when `tenant` is not given.
                                     Defaults to None (platform scope).
            suppress:                If True, return immediately without dispatching.
            unregistered_recipients: Optional list of UnregisteredRecipient - for
                                     recipients who have no User account yet.
            metadata:                Optional dict stored on EVERY created record's
                                     internal-only metadata field (e.g. an
                                     activation_key for delivery-signal receivers).
                                     Never exposed via any serializer.
            delivery_replacements:   Optional marker-to-value substitutions passed
                                     only to the email delivery task. They are not
                                     written to Notification history.

        Returns:
            List of created Notification UUIDs (as strings).
            Empty list if suppress=True or no channels are enabled.

        Raises:
            UnknownEventTypeError: If event_key does not match an active event type.
        """
        if suppress:
            logger.debug("Notification suppressed for event_key=%s", event_key)
            return []

        # The caller's tenant says what the message is ABOUT. It is NOT the
        # owner of the records: a school's ticket notified to platform staff is
        # about the school, but each row belongs to the recipient reading it.
        origin_tenant = tenant or getattr(school, "tenant", None)
        if origin_tenant is None:
            origin_tenant = next(
                (getattr(r, "tenant", None) for r in recipients if getattr(r, "tenant_id", None)),
                None,
            )
        if origin_tenant is None:
            from vs_tenants.models import Tenant
            origin_tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)

        metadata = metadata or {}
        delivery_replacements = {
            str(marker): str(value)
            for marker, value in (delivery_replacements or {}).items()
            if str(marker)
        }

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

        # ── 2. Split the recipients by the tenant that OWNS their records ──
        #        A single send can cross the boundary (a school's ticket goes to
        #        platform triage staff), so ownership is decided per recipient,
        #        never once for the batch.
        all_targets = _build_targets(recipients, unregistered_recipients or [])
        groups = _group_by_owner_tenant(all_targets, origin_tenant)

        # ── 3. Resolve which channels fire, per OWNER tenant (owner row →
        #        platform → default; transactional events bypass settings). The
        #        recipient's own settings decide what reaches the recipient: a
        #        school muting an event must not silence platform staff.
        plans = []
        for owner_tenant, targets in groups:
            enabled_channels = [
                channel
                for channel, on in resolve_channels(event_type, tenant=owner_tenant).items()
                if on
            ]
            if not enabled_channels:
                logger.debug(
                    "All channels disabled for event_key=%s (owner tenant=%s) - "
                    "nothing to dispatch to those recipients.",
                    event_key,
                    getattr(owner_tenant, "slug", None),
                )
                continue
            plans.append((owner_tenant, targets, enabled_channels))

        if not plans:
            return []

        # One template query for the union of channels any group enabled.
        templates = _fetch_templates(
            event_type,
            sorted({channel for _, _, channels in plans for channel in channels}),
        )

        # ── 4. Build Notification records ─────────────────────────────────
        notifications_to_create = []

        for owner_tenant, targets, enabled_channels in plans:
            for target in targets:
                # Each target gets one record per enabled channel.
                for channel in enabled_channels:
                    template = templates.get(channel)
                    if template is None:
                        logger.warning(
                            "No active template for event_key=%s channel=%s - channel skipped.",
                            event_key,
                            channel,
                        )
                        continue

                    # In-app delivery cannot proceed without an account to deliver to.
                    # An UnregisteredRecipient is an email address and nothing else - a
                    # payer, a vendor contact, an invitee - so an in-app row for one has
                    # no inbox it can ever appear in. Several billing events declare both
                    # channels and are only ever sent to unregistered customers, which
                    # was silently producing one unreadable row per send.
                    #
                    # Skipped rather than recorded as FAILED: the no-address case below
                    # records a failure because somebody meant to send an email and it
                    # did not go. Here nobody meant to send anything - the event simply
                    # supports a channel that cannot apply to this recipient - and a
                    # FAILED row would be exactly as unreadable as the SENT one it
                    # replaces. Registered recipients are unaffected, so an event stays
                    # ready for a customer portal without needing its channels changed.
                    if channel == ChannelChoices.IN_APP and isinstance(target, UnregisteredRecipient):
                        logger.debug(
                            "Skipping in-app notification for unregistered recipient on "
                            "event_key=%s - no account to deliver to.",
                            event_key,
                        )
                        continue

                    # Email delivery cannot proceed without an address, but history should record the failure.
                    email_addr = _resolve_email(target)
                    if channel == ChannelChoices.EMAIL and not email_addr:
                        # Pre-flight FAILED - no point rendering or queuing.
                        notifications_to_create.append(
                            _build_failed_notification(
                                event_type=event_type,
                                channel=channel,
                                tenant=owner_tenant,
                                origin_tenant=origin_tenant,
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
                                tenant=owner_tenant,
                                origin_tenant=origin_tenant,
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
                            tenant=owner_tenant,
                            origin_tenant=origin_tenant,
                            target=target,
                            subject=rendered_subject,
                            body=rendered_body,
                            html_body=rendered_html if not is_in_app else "",
                            metadata=metadata,
                            # IN_APP is immediately SENT - no async task needed
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
                        if delivery_replacements:
                            deliver_email_notification.delay(
                                notif_id,
                                replacements=delivery_replacements,
                            )
                        else:
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
    no entry in the dict - callers handle the None case.
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


# Decide which tenant OWNS the records for each target, and group by it.
def _group_by_owner_tenant(targets: list, origin_tenant) -> list:
    """
    Return [(owner_tenant, [targets])], preserving the caller's recipient order.

    A registered recipient's records belong to that recipient's own tenant:
    that is the inbox they read and the history log they appear in. An
    UnregisteredRecipient has no account and so no tenant of its own, so its
    records stay with the originating tenant - a vendor's purchase-order email
    belongs in the school's history, which is where the person chasing that
    delivery goes looking for it.
    """
    grouped: dict = {}
    for target in targets:
        owner_id = getattr(target, "tenant_id", None) or origin_tenant.pk
        grouped.setdefault(owner_id, []).append(target)

    # Resolve the owner tenants in ONE query. Reading target.tenant per
    # recipient would be a lazy fetch each time - and a triage queue is a list
    # of people who all share the same tenant.
    tenants = {origin_tenant.pk: origin_tenant}
    unresolved = [pk for pk in grouped if pk not in tenants]
    if unresolved:
        from vs_tenants.models import Tenant
        tenants.update({t.pk: t for t in Tenant.objects.filter(pk__in=unresolved)})

    return [(tenants[pk], members) for pk, members in grouped.items()]


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
    event_type, channel, tenant, origin_tenant, target, subject, body, html_body,
    metadata, status, dispatched_at,
) -> Notification:
    """
    Construct an unsaved Notification instance.
    Handles both registered Users and UnregisteredRecipient targets.

    ``tenant`` owns the row (the recipient's own tenant); ``origin_tenant``
    records what it is about. Both are required, so no caller can create a row
    that is quietly owned by the wrong side of a cross-tenant event.
    """
    is_unregistered = isinstance(target, UnregisteredRecipient)
    return Notification(
        event_type=event_type,
        channel=channel,
        tenant=tenant,
        origin_tenant=origin_tenant,
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
    event_type, channel, tenant, origin_tenant, target, failure_reason: str,
    metadata: dict,
) -> Notification:
    """
    Construct an unsaved Notification instance pre-set to FAILED.
    Used for pre-flight failures (no email address, render error)
    where no Celery task should be enqueued.

    A failure is history too, and history is read by whoever owns the row, so
    it follows the same ownership rule as a delivered record.
    """
    is_unregistered = isinstance(target, UnregisteredRecipient)
    return Notification(
        event_type=event_type,
        channel=channel,
        tenant=tenant,
        origin_tenant=origin_tenant,
        recipient=None if is_unregistered else target,
        unregistered_email=target.email if is_unregistered else "",
        subject="",
        body="",
        html_body="",
        metadata=metadata,
        status=NotificationStatus.FAILED,
        failure_reason=failure_reason,
    )
