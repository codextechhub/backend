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

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger('vs_user.tasks')

_INVITATION_TOKEN_MARKER = "__XVS_INVITATION_TOKEN__"
_PASSWORD_RESET_TOKEN_MARKER = "__XVS_PASSWORD_RESET_TOKEN__"


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
      - The school name (or a school-less variant when the user has no school)
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

    school = getattr(user.tenant, 'school_profile', None)  # None for platform users.
    school_name = school.name if school else 'CodeX'
    base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
    if not base_url:
        raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')

    invitation_url = f'{base_url.rstrip("/")}/activate/{_INVITATION_TOKEN_MARKER}'

    send_notification(
        event_key="user.invited",
        context={
            'user_first_name': user.first_name,
            'user_full_name':  user.full_name,
            'school_name':     school_name,
            'invitation_url':  invitation_url,
            'expiry_days':     7,
            # Drives the school-less subject variant in the DB template.
            'has_school':      bool(school),
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
      SELF  - user requested it themselves. Link valid for 1 hour.
      ADMIN - admin triggered it. Link valid for 24 hours.

    The raw token is embedded in the reset URL. It is never stored in the
    database - only its SHA-256 hash is stored in PasswordResetRequest.

    From-address parity: sender_name is carried in metadata as from_name so the
    delivery task builds the From from it.
    """
    from vs_notifications.notify import send_notification

    from .action_tokens import password_reset_token_digest
    from .models import PasswordResetRequest

    try:
        reset_request = (
            PasswordResetRequest.objects
            .select_related('user')
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
    reset_url    = f'{base_url.rstrip("/")}/reset-password/{_PASSWORD_RESET_TOKEN_MARKER}'
    expiry_hours = 1 if origin == 'SELF' else 24

    send_notification(
        event_key="user.password_reset",
        context={
            'user_first_name': user.first_name,
            'reset_url':       reset_url,
            'expiry_hours':    expiry_hours,
            'origin':          origin,
            'sender_name':     sender_name,
        },
        recipients=[user],
        tenant=user.tenant,
        metadata={'from_name': sender_name},
        delivery_replacements={_PASSWORD_RESET_TOKEN_MARKER: token},
    )
    logger.info('Password reset email dispatched for %s (origin=%s)', user.email, origin)
