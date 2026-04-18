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
from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

logger = logging.getLogger('vs_user.tasks')


# =============================================================================
# SECTION 1 — INVITATION EMAIL
# =============================================================================

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
        base_url         = getattr(settings, 'FRONTEND_BASE_URL', 'https://vision.codexng.com')

        # The invitation link uses the user's ID — no token needed.
        invitation_url = f'{base_url}/v1/user/auth/activate/{user.activation_key}/preview/'

        context = {
            'user':             user,
            'school_name': school_name,
            'invitation_url':   invitation_url,
            'expiry_days':      7,
        }

        # TODO: Replace send_mail with Notification Engine (Module 7) once available.
        html_message  = render_to_string('vs_user/emails/invitation.html', context)
        plain_message = render_to_string('vs_user/emails/invitation.txt', context)

        subject = f'You have been invited to {school_name} on X Vision Systems' if user.school else 'You have been invited to X Vision Systems'

        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
        )

        logger.info(f'Invitation email sent to {user.email}')
        
    except User.DoesNotExist:
        logger.error(f'send_invitation_email_task: User {user.email} not found')
        # Don't retry for non-existent users
        return
        
    except SMTPException as smtp_exc:
        logger.error(f'SMTP error sending invitation to user {user.email}: {smtp_exc}')
        raise self.retry(exc=smtp_exc)
    
    except Exception as exc:
        logger.error(f'send_invitation_email_task failed for user_id={user.email}: {exc}')
        raise self.retry(exc=exc)


# =============================================================================
# SECTION 2 — PASSWORD RESET EMAIL
# =============================================================================

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email_task(self, activation_key: str, origin: str):
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

        base_url      = getattr(settings, 'FRONTEND_BASE_URL', 'https://vision.codexng.com')
        reset_url     = f'{base_url}v1/user/auth/reset-password/{activation_key}/preview/'
        expiry_hours  = 1 if origin == 'SELF' else 24

        context = {
            'user':          user,
            'reset_url':     reset_url,
            'expiry_hours':  expiry_hours,
            'origin':        origin,
        }

        # TODO: Replace send_mail with Notification Engine (Module 7) once available.
        html_message  = render_to_string('vs_user/emails/password_reset.html', context)
        plain_message = render_to_string('vs_user/emails/password_reset.txt', context)

        subject = (
            'Reset your CodeX Vision password'
            if origin == 'SELF'
            else 'Your CodeX Vision password has been reset by an administrator'
        )

        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
        )

        logger.info(f'Password reset email sent to {user.email} (origin={origin})')

    except Exception as exc:
        logger.error(f'send_password_reset_email_task failed for user_id={user.id}: {exc}')
        raise self.retry(exc=exc)