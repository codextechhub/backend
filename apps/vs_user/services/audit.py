from __future__ import annotations

import logging

from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

logger = logging.getLogger('vs_user.audit')

from vs_audit.models import AuditModuleKey, AuditActionType, AuditStatus
from vs_audit.services import emit_audit_event

from ..models import AuthEventLog

# Maps AuthEventLog.Event strings → AuditActionType choices.
_AUTH_EVENT_TO_ACTION: dict[str, str] = {
    AuthEventLog.Event.USER_CREATED:             AuditActionType.USER_CREATED,
    AuthEventLog.Event.INVITATION_SENT:          AuditActionType.USER_INVITED,
    AuthEventLog.Event.ACCOUNT_ACTIVATED:        AuditActionType.ACCOUNT_ACTIVATED,
    AuthEventLog.Event.LOGIN_SUCCESS:            AuditActionType.LOGIN_SUCCESS,
    AuthEventLog.Event.LOGIN_FAILURE:            AuditActionType.LOGIN_FAILED,
    AuthEventLog.Event.TOKEN_REVOKED:            AuditActionType.TOKEN_REVOKED,
    AuthEventLog.Event.FORCE_LOGOUT:             AuditActionType.FORCE_LOGOUT,
    AuthEventLog.Event.ACCOUNT_LOCKED:           AuditActionType.ACCOUNT_LOCKED,
    AuthEventLog.Event.ACCOUNT_UNLOCKED:         AuditActionType.ACCOUNT_UNLOCKED,
    AuthEventLog.Event.ACCOUNT_SUSPENDED:        AuditActionType.ACCOUNT_SUSPENDED,
    AuthEventLog.Event.ACCOUNT_REACTIVATED:      AuditActionType.ACCOUNT_REACTIVATED,
    AuthEventLog.Event.ACCOUNT_DEACTIVATED:      AuditActionType.ACCOUNT_DEACTIVATED,
    AuthEventLog.Event.PASSWORD_RESET_REQUESTED: AuditActionType.PASSWORD_RESET_REQUESTED,
    AuthEventLog.Event.PASSWORD_RESET_COMPLETED: AuditActionType.PASSWORD_RESET,
    AuthEventLog.Event.PASSWORD_CHANGED:         AuditActionType.PASSWORD_CHANGED,
    AuthEventLog.Event.EMAIL_CHANGED:            AuditActionType.EMAIL_CHANGED,
}

# Events that represent a failure outcome.
_FAILED_EVENTS = {
    AuthEventLog.Event.LOGIN_FAILURE,
    AuthEventLog.Event.ACCOUNT_LOCKED,
}


def log_auth_event(*, actor, subject, school, event: str, request=None, metadata: dict | None = None):
    """
    Emit a single identity/auth action as a vs_audit AuditEvent.

    Never raises — a logging failure must never prevent an auth action from completing.
    actor: the user who performed the action (may be None for system-initiated events).
    subject: the user the action was performed on.
    school: the tenant context (stored in metadata).
    """
    action_type = _AUTH_EVENT_TO_ACTION.get(event, AuditActionType.CUSTOM)
    status = AuditStatus.FAILED if event in _FAILED_EVENTS else AuditStatus.SUCCESS

    extra_meta = metadata or {}
    extra_meta["auth_event"] = event
    if school:
        extra_meta["school_id"] = str(school.pk)
        extra_meta["school_slug"] = getattr(school, "slug", "")
    if request:
        extra_meta["ip_address"] = get_client_ip(request)
        extra_meta["user_agent"] = request.META.get("HTTP_USER_AGENT", "")

    entity = subject or actor
    entity_id = str(entity.pk) if entity else "unknown"
    entity_label = getattr(entity, "full_name", "") or getattr(entity, "email", "") or getattr(entity, "username", "") if entity else ""

    try:
        emit_audit_event(
            module_key=AuditModuleKey.IDENTITY,
            action_type=action_type,
            actor_user=actor,
            entity_type="User",
            entity_id=entity_id,
            entity_label=entity_label,
            status=status,
            metadata=extra_meta,
        )
    except Exception as exc:
        logger.critical('log_auth_event failed — audit trail may be incomplete: %s', exc, exc_info=True)


def record_attempt(*, email_entered, user=None,
                   tenant=None, result: str, failure_code: str = '',
                   request=None, metadata: dict | None = None):
    """
    Write a single record to AuthAttempt for rate-limiting and lockout tracking.
    Kept separate from audit events because it drives security policy (not compliance).
    """
    from ..models import AuthAttempt
    try:
        AuthAttempt.objects.create(
            email_entered=email_entered,
            user=user,
            tenant=tenant,
            ip_address=get_client_ip(request) if request else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
            result=result,
            failure_code=failure_code or '',
            metadata=metadata or {},
        )
    except Exception as exc:
        logger.critical('record_attempt failed — auth attempt not recorded: %s', exc, exc_info=True)


def blacklist_all_user_tokens(user):
    """
    Blacklists all outstanding SimpleJWT refresh tokens for the given user.
    Ends all active sessions cryptographically.
    Called on: suspend, deactivate, force logout, password reset, email change.
    """
    from vs_admin_console.services import end_impersonations_for_user
    end_impersonations_for_user(user)
    tokens = OutstandingToken.objects.filter(user=user)
    for token in tokens:
        BlacklistedToken.objects.get_or_create(token=token)


def blacklist_token_by_jti(jti: str) -> bool:
    """
    Blacklists a single outstanding SimpleJWT refresh token by its JTI.
    Returns True when a matching token was found and blacklisted (or already was).
    Used when a single LoginSession is force-ended so that just that device is
    cryptographically signed out without touching the user's other sessions.
    """
    if not jti:
        return False
    try:
        token = OutstandingToken.objects.get(jti=jti)
    except OutstandingToken.DoesNotExist:
        return False
    BlacklistedToken.objects.get_or_create(token=token)
    return True


def get_client_ip(request) -> str | None:
    """
    Extracts the real client IP, handling reverse proxy X-Forwarded-For headers.
    Tolerates request=None (service-layer callers without an HTTP request).
    """
    if request is None:
        return None
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _get_ch_header(request, header: str) -> str:
    """Return a cleaned Client Hints header value, or empty string if absent/unknown."""
    if not request:
        return ''
    value = request.META.get(header, '').strip().strip('"')
    return value if value and value.lower() not in ('', 'unknown') else ''


def get_device_label(user_agent_string: str, request=None) -> str:
    """
    Build a human-readable device label from the UA string and optional Client Hints.

    Modern Android Chrome sends 'K' as the device model (UA Reduction privacy change).
    For those cases we fall back to Sec-CH-UA-Model if the client sent it, otherwise
    we surface just the OS name so the label is never misleadingly wrong.
    """
    if not user_agent_string:
        return 'Unknown Device'
    try:
        import user_agents
        ua = user_agents.parse(user_agent_string)

        # ── Browser ───────────────────────────────────────────────────────────
        browser = ua.browser.family or 'Unknown Browser'
        # "Mobile Safari" on its own means Safari; keep it when paired with Chrome.
        if 'Chrome' not in browser:
            browser = browser.replace('Mobile Safari', 'Safari')

        # ── Device / OS ───────────────────────────────────────────────────────
        if ua.is_pc:
            os_name = ua.os.family or ''
            if 'Mac' in os_name:
                os_label = 'macOS'
            elif 'Windows' in os_name:
                os_label = 'Windows'
            elif 'Chrome' in os_name:
                os_label = 'ChromeOS'
            else:
                os_label = os_name or 'Unknown OS'
            device_class = 'desktop'

        elif ua.is_tablet:
            ch_model = _get_ch_header(request, 'HTTP_SEC_CH_UA_MODEL')
            if ch_model:
                os_label = ch_model
            elif ua.device.model and ua.device.model not in ('K', 'Other', ''):
                brand = ua.device.brand or ''
                model = ua.device.model
                os_label = f"{brand} {model}".strip() if brand and brand != 'Other' else model
            else:
                ch_platform = _get_ch_header(request, 'HTTP_SEC_CH_UA_PLATFORM')
                os_label = ch_platform or ua.os.family or 'Tablet'
            device_class = 'tablet'

        elif ua.is_mobile:
            # Client Hints carry the real model on modern Android Chrome (where UA = "K").
            ch_model = _get_ch_header(request, 'HTTP_SEC_CH_UA_MODEL')
            if ch_model:
                os_label = ch_model
            elif ua.device.model and ua.device.model not in ('K', 'Other', ''):
                brand = ua.device.brand or ''
                model = ua.device.model
                os_label = f"{brand} {model}".strip() if brand and brand != 'Other' else model
            else:
                ch_platform = _get_ch_header(request, 'HTTP_SEC_CH_UA_PLATFORM')
                os_label = ch_platform or ua.os.family or 'Mobile'
            device_class = 'mobile'

        else:
            os_label = ua.os.family or 'Unknown'
            device_class = 'unknown'

        return f"Browser: {browser} · OS: {os_label} · Class: {device_class}"

    except Exception:
        return user_agent_string[:80] if user_agent_string else 'Unknown Device'
