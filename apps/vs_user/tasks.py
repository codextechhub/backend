# tasks.py
# Celery tasks for vs_users.
# All email dispatch is async — tasks never block an HTTP request.
# Tasks retry up to 3 times on failure with a 60-second delay.
#
# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — INVITATION EMAIL
# ─────────────────────────────────────────────────────────────────────────────
# Dispatched when a new user account is created or when an admin resends.
# The invitation link is: {FRONTEND_BASE_URL}/invite/{user.id}/
# No token is embedded — the user's UUID is the identifier.
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
from django.template.loader import render_to_string

from core.mail import build_from_email, send_email

logger = logging.getLogger('vs_user.tasks')


# =============================================================================
# SECTION 1 — INVITATION EMAIL
# =============================================================================

def _record_invitation_email(activation_key: str, *, success: bool, error: str = '', sent_at=None):
    """Update the UserInvitation email tracking fields. Never raises."""
    try:
        from .models import UserInvitation
        inv = UserInvitation.objects.get(user__activation_key=activation_key)
        inv.email_attempts += 1
        if success:
            inv.email_status    = UserInvitation.EmailStatus.SENT
            inv.email_sent_at   = sent_at
            inv.email_last_error = ''
        else:
            inv.email_last_error = error
        inv.save(update_fields=['email_attempts', 'email_status', 'email_sent_at', 'email_last_error', 'updated_at'])
    except Exception:
        logger.exception('Failed to update invitation email status for activation_key=%s', activation_key)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_invitation_email_task(self, activation_key: str):
    """
    Sends the account activation email to a newly created user.

    The email contains:
      - The user's full name (so they can confirm the account is for them)
      - The invitation link: {FRONTEND_BASE_URL}/invite/{user.activation_key}/
      - The school name
      - An expiry notice (7 days)

    The URL is the same on resend — only the expiry window is extended.
    """
    try:
        from .models import User
        user = User.objects.select_related('school').get(activation_key=activation_key)

        school_name = user.school.name if user.school else 'CodeX'
        base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
        if not base_url:
            raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')

        invitation_url = f'{base_url}/v1/user/auth/activate/{user.activation_key}/preview/'

        context = {
            'user':           user,
            'school_name':    school_name,
            'invitation_url': invitation_url,
            'expiry_days':    7,
        }

        html_message  = render_to_string('vs_user/emails/invitation.html', context)
        plain_message = render_to_string('vs_user/emails/invitation.txt', context)

        subject = (
            f'You have been invited to {school_name} on X Vision Systems'
            if user.school else
            'You have been invited to X Vision Systems'
        )

        send_email(
            subject=subject,
            plain_message=plain_message,
            html_message=html_message,
            from_email=build_from_email(user.invited_by_name or None),
            recipient_list=[user.email],
        )

        from django.utils import timezone as tz
        _record_invitation_email(activation_key, success=True, sent_at=tz.now())
        logger.info('Invitation email sent to %s', user.email)

    except User.DoesNotExist:
        logger.error('send_invitation_email_task: no user with activation_key=%s', activation_key)
        return

    except Exception as exc:
        error_str = str(exc)
        is_final  = self.request.retries >= self.max_retries
        _record_invitation_email(activation_key, success=False, error=error_str)
        if is_final:
            from .models import UserInvitation
            try:
                inv = UserInvitation.objects.get(user__activation_key=activation_key)
                inv.email_status = UserInvitation.EmailStatus.FAILED
                inv.save(update_fields=['email_status', 'updated_at'])
            except Exception:
                pass
            logger.error('Invitation email permanently failed for %s: %s', activation_key, error_str)
            return
        logger.warning('Invitation email attempt %s failed for %s: %s', self.request.retries + 1, activation_key, error_str)
        raise self.retry(exc=exc)


# =============================================================================
# SECTION 2 — PASSWORD RESET EMAIL
# =============================================================================

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email_task(self, activation_key: str, origin: str, sender_name: str = 'CodeX System'):
    """
    Sends a password reset email.

    origin values:
      SELF  — user requested it themselves. Link valid for 1 hour.
      ADMIN — admin triggered it. Link valid for 24 hours.

    The raw token is embedded in the reset URL. It is never stored in the
    database — only its SHA-256 hash is stored in PasswordResetRequest.
    """
    try:
        from .models import User
        user = User.objects.get(activation_key=activation_key)

        base_url = getattr(settings, 'FRONTEND_BASE_URL', None)
        if not base_url:
            raise ImproperlyConfigured('FRONTEND_BASE_URL must be set in settings.')
        reset_url     = f'{base_url.rstrip("/")}/reset-password/{activation_key}'
        expiry_hours  = 1 if origin == 'SELF' else 24

        context = {
            'user':          user,
            'reset_url':     reset_url,
            'expiry_hours':  expiry_hours,
            'origin':        origin,
            'sender_name':   sender_name,
        }

        # TODO: Replace send_mail with Notification Engine (Module 7) once available.
        html_message  = render_to_string('vs_user/emails/password_reset.html', context)
        plain_message = render_to_string('vs_user/emails/password_reset.txt', context)

        subject = (
            'Reset your CodeX Vision password'
            if origin == 'SELF'
            else 'Your CodeX Vision password has been reset by an administrator'
        )

        send_email(
            subject=subject,
            plain_message=plain_message,
            html_message=html_message,
            from_email=build_from_email(sender_name),
            recipient_list=[user.email],
        )

        logger.info(f'Password reset email sent to {user.email} (origin={origin})')

    except User.DoesNotExist:
        logger.error(f'send_password_reset_email_task: no user with activation_key={activation_key}')
        return
    except Exception as exc:
        logger.error(f'send_password_reset_email_task failed for activation_key={activation_key}: {exc}')
        raise self.retry(exc=exc)