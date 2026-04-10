# services/audit.py
# Centralised audit and session helpers.
# Every other service calls these — they must never import from other services
# to avoid circular dependencies.

from __future__ import annotations

from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

from ..models import AuthEventLog


def log_auth_event(*, actor, subject, school, event: str, request=None, metadata: dict | None = None):
    """
    Write a single record to AuthEventLog.
    Never raises — a logging failure must never prevent an auth action
    from completing. Errors are caught and sent to the error tracker.
    """
    try:
        AuthEventLog.objects.create(
            actor=actor,
            subject=subject,
            school=school,
            event=event,
            ip_address=get_client_ip(request) if request else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
            metadata=metadata or {},
        )
    except Exception as exc:
        import logging
        logging.getLogger('vs_users.audit').error(
            f'log_auth_event failed for event={event}: {exc}'
        )


def record_attempt(*, email_entered, school_context='', user=None,
                   school=None, result: str, failure_code: str = '',
                   request=None, metadata: dict | None = None):
    """
    Write a single record to AuthAttempt.
    Called on every login attempt — success, failure, and blocked.
    """
    from ..models import AuthAttempt
    try:
        AuthAttempt.objects.create(
            email_entered=email_entered,
            school_context=school_context or '',
            user=user,
            school=school,
            ip_address=get_client_ip(request) if request else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
            result=result,
            failure_code=failure_code or '',
            metadata=metadata or {},
        )
    except Exception as exc:
        import logging
        logging.getLogger('vs_users.audit').error(
            f'record_attempt failed: {exc}'
        )


def blacklist_all_user_tokens(user):
    """
    Blacklists all outstanding SimpleJWT refresh tokens for the given user.
    Ends all active sessions cryptographically.
    Called on: suspend, deactivate, force logout, password reset, email change.
    """
    tokens = OutstandingToken.objects.filter(user=user)
    for token in tokens:
        BlacklistedToken.objects.get_or_create(token=token)


def get_client_ip(request) -> str | None:
    """
    Extracts the real client IP, handling reverse proxy X-Forwarded-For headers.
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')