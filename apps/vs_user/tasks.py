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
# SECTION 1 — INVITATION EMAIL
# ─────────────────────────────────────────────────────────────────────────────
# Dispatched when a new user account is created or when an admin resends.
# The invitation link is: {FRONTEND_BASE_URL}/activate/{user.activation_key}
# No token is embedded — the user's activation_key is the identifier.
#
# SECTION 2 — PASSWORD RESET EMAIL
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


# =============================================================================
# SECTION 1 — INVITATION EMAIL
# =============================================================================

@shared_task(bind=True, name="vs_user.send_invitation_email_task")
def send_invitation_email_task(self, activation_key: str):
    """
    Dispatch the account activation email via the notification engine.

    The engine's user.invited template renders:
      - The user's first name / full name
      - The invitation link: {FRONTEND_BASE_URL}/activate/{user.activation_key}
      - The school name (or a school-less variant when the user has no school)
      - The 7-day expiry notice

    The URL is the same on resend — only the expiry window is extended.

    From-address parity: the inviter's display name is carried in metadata as
    from_name so the delivery task builds the From from it. Delivery tracking
    (UserInvitation.email_*) is updated by the receivers in vs_user/receivers.py,
    correlated via metadata.activation_key.
    """
    from vs_notifications.notify import send_notification

    from .models import User

    try:
        user = User.objects.select_related('tenant__school_profile').get(activation_key=activation_key)
    except User.DoesNotExist:
        logger.error('send_invitation_email_task: no user with activation_key=%s', activation_key)
        return

    school = getattr(user.tenant, 'school_profile', None)  # None for platform users.
    school_name = school.name if school else 'CodeX'
    base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
    if not base_url:
        raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')

    invitation_url = f'{base_url.rstrip("/")}/activate/{user.activation_key}'

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
            'activation_key': str(user.activation_key),
            'from_name':      user.invited_by_name or None,
        },
    )
    logger.info('Invitation email dispatched for %s', user.email)


# =============================================================================
# SECTION 2 — PASSWORD RESET EMAIL
# =============================================================================

@shared_task(bind=True, name="vs_user.send_password_reset_email_task")
def send_password_reset_email_task(self, activation_key: str, origin: str, sender_name: str = 'CodeX System'):
    """
    Dispatch a password reset email via the notification engine.

    origin values:
      SELF  — user requested it themselves. Link valid for 1 hour.
      ADMIN — admin triggered it. Link valid for 24 hours.

    The raw token is embedded in the reset URL. It is never stored in the
    database — only its SHA-256 hash is stored in PasswordResetRequest.

    From-address parity: sender_name is carried in metadata as from_name so the
    delivery task builds the From from it.
    """
    from vs_notifications.notify import send_notification

    from .models import User

    try:
        user = User.objects.get(activation_key=activation_key)
    except User.DoesNotExist:
        logger.error('send_password_reset_email_task: no user with activation_key=%s', activation_key)
        return

    base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
    if not base_url:
        raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')
    reset_url    = f'{base_url.rstrip("/")}/reset-password/{activation_key}'
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
    )
    logger.info('Password reset email dispatched for %s (origin=%s)', user.email, origin)
