# tasks.py
# Celery tasks for vs_users.
#
# Both tasks are thin wrappers around the vs_notifications engine. They exist
# as tasks (rather than inline send_notification calls at the call sites) for
# two reasons the call sites depend on:
#   * they are enqueued with .delay() carrying the reserved _job_* kwargs, so
#     core.tasks_base.TrackedTask records a BackgroundJob row for each email;
#   * the async hop keeps the (cheap, synchronous) dispatch off the request.
#
# Nothing enqueues these tasks directly. Callers go through the queue_* helpers
# below, which hold the enqueue until the caller's transaction commits - see
# queue_invitation_email for why enqueuing any earlier silently loses emails.
#
# The engine renders DB templates, creates the Notification record, and sends
# the email inside vs_notifications.deliver_email_notification. Invitation email
# delivery tracking (UserInvitation.email_*) is updated by the delivery-signal
# receivers in vs_user/receivers.py.
#
# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 - INVITATION EMAIL
# ─────────────────────────────────────────────────────────────────────────────
# Dispatched when a new user account is created or when an admin resends.
# The invitation link contains a one-time invitation-family token. Only its
# HMAC-SHA-256 digest is stored on UserInvitation.
#
# SECTION 2 - PASSWORD RESET EMAIL
# ─────────────────────────────────────────────────────────────────────────────
# Dispatched for both self-service and admin-triggered password resets.
# The raw token (never stored) is embedded in the reset link.
# Messaging adapts based on origin: SELF or ADMIN.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from math import ceil

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('vs_user.tasks')

_INVITATION_TOKEN_MARKER = "__XVS_INVITATION_TOKEN__"
_PASSWORD_RESET_TOKEN_MARKER = "__XVS_PASSWORD_RESET_TOKEN__"


def _tenant_display_name(user) -> str:
    """Return the product-facing workspace name for an account's tenant."""
    profile = getattr(user.tenant, 'school_profile', None)
    if profile is not None:
        return profile.name
    if getattr(user.tenant, 'kind', None) == 'PLATFORM':
        return 'CodeX Vision'
    return user.tenant.name


# =============================================================================
# SECTION 1 - INVITATION EMAIL
# =============================================================================

@shared_task(bind=True, name="vs_user.send_invitation_email_task")
def send_invitation_email_task(self, invitation_id: int, token: str):
    """
    Dispatch the account activation email via the notification engine.

    The engine's user.invited template renders:
      - The user's first name / full name
      - The invitation link containing the raw one-time token
      - The workspace name and the person who sent the invitation
      - The 7-day expiry notice

    A resend rotates the token, so an earlier URL stops working immediately.

    From-address parity: the inviter's display name is carried in metadata as
    from_name so the delivery task builds the From from it. Delivery tracking
    (UserInvitation.email_*) is updated by the receivers in vs_user/receivers.py,
    correlated via metadata.invitation_id. The raw token is never copied into
    notification metadata.
    """
    from vs_notifications.notify import send_notification

    from .action_tokens import invitation_token_digest
    from .models import UserInvitation

    try:
        invitation = (
            UserInvitation.objects
            .select_related('user__tenant__school_profile')
            .get(pk=invitation_id)
        )
    except UserInvitation.DoesNotExist:
        logger.error('send_invitation_email_task: no invitation with id=%s', invitation_id)
        return

    if invitation_token_digest(token) != invitation.token_hash or not invitation.is_valid:
        logger.info('send_invitation_email_task: invitation %s is no longer valid', invitation_id)
        return

    user = invitation.user

    profile = getattr(user.tenant, 'school_profile', None)  # Legacy template context below.
    tenant_name = _tenant_display_name(user)
    base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
    if not base_url:
        raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')

    invitation_url = f'{base_url.rstrip("/")}/activate/{_INVITATION_TOKEN_MARKER}'

    send_notification(
        event_key="user.invited",
        context={
            'user_first_name': user.first_name,
            'user_full_name':  user.full_name,
            'tenant_name':     tenant_name,
            'inviter_name':    user.invited_by_name or '',
            'invitation_url':  invitation_url,
            'expiry_days':     7,
            # Compatibility for staff-authored templates based on the earlier
            # standard copy. New standard templates use domain-neutral keys.
            'school_name':     tenant_name,
            'has_school':      bool(profile),
        },
        recipients=[user],
        tenant=user.tenant,
        metadata={
            'invitation_id': invitation.pk,
            'from_name':      user.invited_by_name or None,
        },
        delivery_replacements={_INVITATION_TOKEN_MARKER: token},
    )
    logger.info('Invitation email dispatched for %s', user.email)


def queue_invitation_email(*, invitation_id, token, owner_id=None, label):
    """Queue the invitation email for after the caller's transaction commits.

    Every caller writes the invitation row inside a transaction and then asks
    for the email. Enqueuing from inside that transaction hands the worker a
    primary key the database has not published yet, and the two race: a worker
    that picks the job up before the commit lands finds no row, logs
    "no invitation with id=...", and returns. The admin who clicked Resend is
    told the invitation went out, and it never did.

    ``on_commit`` runs the enqueue once the row is durable, so the worker can
    always see what it was sent to fetch - and drops the email entirely if the
    caller rolls back, which is right too: the invitation it advertises does
    not exist.

    A broker failure is logged and swallowed. The row is already committed by
    the time this runs, so raising could not undo it; the invitation stands and
    can be resent.
    """
    def _dispatch():
        try:
            send_invitation_email_task.delay(
                invitation_id=invitation_id,
                token=token,
                # The job belongs to whoever asked for the invite, not the
                # invitee: a bulk approval must not drop queue rows and
                # completion notifications into 200 strangers' inboxes.
                _job_owner_id=owner_id,
                _job_label=label,
                _job_kind="email",
                # Fan-out plumbing: one bell notification per invited row is spam.
                _job_notify=False,
            )
        except Exception:
            logger.error(
                'Failed to dispatch invitation email for invitation %s - it will '
                'need to be resent manually.',
                invitation_id, exc_info=True,
            )

    transaction.on_commit(_dispatch)


# =============================================================================
# SECTION 2 - PASSWORD RESET EMAIL
# =============================================================================

@shared_task(bind=True, name="vs_user.send_password_reset_email_task")
def send_password_reset_email_task(
    self, reset_request_id: int, token: str, origin: str,
    sender_name: str = 'CodeX System',
):
    """
    Dispatch a password reset email via the notification engine.

    origin values:
      SELF  - user requested it themselves. Uses the configured self-service window.
      ADMIN - admin triggered it. Uses the configured administrator window.

    The raw token is embedded in the reset URL. It is never stored in the
    database - only its HMAC-SHA-256 digest is stored in PasswordResetRequest.

    From-address parity: sender_name is carried in metadata as from_name so the
    delivery task builds the From from it.
    """
    from vs_notifications.notify import send_notification

    from .action_tokens import password_reset_token_digest
    from .models import PasswordResetRequest

    try:
        reset_request = (
            PasswordResetRequest.objects
            .select_related('user__tenant__school_profile')
            .get(pk=reset_request_id)
        )
    except PasswordResetRequest.DoesNotExist:
        logger.error('send_password_reset_email_task: no request with id=%s', reset_request_id)
        return

    if (
        password_reset_token_digest(token) != reset_request.token_hash
        or not reset_request.is_valid
    ):
        logger.info('send_password_reset_email_task: request %s is no longer valid', reset_request_id)
        return

    user = reset_request.user

    base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
    if not base_url:
        raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')
    reset_url = f'{base_url.rstrip("/")}/reset-password/{_PASSWORD_RESET_TOKEN_MARKER}'
    expiry_hours = max(
        1,
        ceil((reset_request.expires_at - reset_request.created_at).total_seconds() / 3600),
    )
    expires_at = timezone.localtime(reset_request.expires_at).strftime(
        '%d %b %Y, %H:%M %Z'
    )

    send_notification(
        event_key="user.password_reset",
        context={
            'user_first_name': user.first_name,
            'user_email':      user.email,
            'tenant_name':     _tenant_display_name(user),
            'reset_url':       reset_url,
            'expiry_hours':    expiry_hours,
            'expires_at':      expires_at,
            'origin':          origin,
            'sender_name':     sender_name,
        },
        recipients=[user],
        tenant=user.tenant,
        metadata={'from_name': sender_name},
        delivery_replacements={_PASSWORD_RESET_TOKEN_MARKER: token},
    )
    logger.info('Password reset email dispatched for %s (origin=%s)', user.email, origin)


def queue_password_reset_email(*, reset_request_id, token, origin,
                               sender_name='CodeX System', owner_id=None):
    """Queue the reset email for after the caller's transaction commits.

    Same race as :func:`queue_invitation_email`, with a worse ending: the
    caller is a user who asked for a reset link, got a successful response,
    and would simply never receive an email - the worker looked for the reset
    row before the commit that created it was visible, found nothing, and
    returned. ``on_commit`` is what makes the row older than the job.
    """
    def _dispatch():
        try:
            try:
                send_password_reset_email_task.delay(
                    reset_request_id=reset_request_id,
                    token=token,
                    origin=origin,
                    sender_name=sender_name,
                    _job_owner_id=owner_id,
                    _job_label="Password reset email",
                    _job_kind="email",
                )
            except Exception:
                # Broker unavailable - run synchronously so the email still goes out.
                send_password_reset_email_task.apply(
                    kwargs=dict(
                        reset_request_id=reset_request_id,
                        token=token,
                        origin=origin,
                        sender_name=sender_name,
                    )
                )
        except Exception:
            # The reset row is already committed - an email failure must never
            # break the reset request itself. The user can request again.
            logger.exception(
                'Password reset email dispatch failed for reset request %s',
                reset_request_id,
            )

    transaction.on_commit(_dispatch)
