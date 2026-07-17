from __future__ import annotations

import re

from django.utils import timezone

from .context import clear_request_context, get_current_audit_event_count


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# These writes only maintain the current user's inbox/read state. They are
# automatic UI bookkeeping, not business changes, so successful calls do not
# belong in the audit timeline. Failed calls remain security-audited below.
NON_BUSINESS_PROXY_WRITE_PATHS = {
    "/v1/notify/mark-read/",
    "/v1/notify/mark-all-read/",
    "/v1/notify/acknowledge-route/",
}

# Distinct paths kept per session; existing entries keep counting past the cap.
ACCESS_LOG_MAX_PATHS = 200


def _record_proxy_activity(session, request, response):
    """Mark the session as live and add successful reads to its access trail.

    Writes and failures already land in the audit stream; the trail records
    what data the proxier viewed, deduped by path so browsing stays readable.
    Never raises — bookkeeping must not break the proxied response.
    """
    try:
        now = timezone.now()
        session.last_activity_at = now
        update_fields = ["last_activity_at"]
        if request.method in SAFE_METHODS and response.status_code < 400:
            log = list(session.access_log or [])
            entry = next((e for e in log if e.get("path") == request.path), None)
            if entry is not None:
                entry["count"] = int(entry.get("count", 0)) + 1
                entry["last_at"] = now.isoformat()
                update_fields.append("access_log")
            elif len(log) < ACCESS_LOG_MAX_PATHS:
                log.append({
                    "path": request.path,
                    "count": 1,
                    "first_at": now.isoformat(),
                    "last_at": now.isoformat(),
                })
                update_fields.append("access_log")
            session.access_log = log
        session.save(update_fields=update_fields)
    except Exception:  # pragma: no cover — defensive; see docstring.
        pass


def _user_label(user) -> str:
    if user is None:
        return "Unknown user"
    return (
        getattr(user, "full_name", None)
        or getattr(user, "get_full_name", lambda: "")()
        or getattr(user, "email", None)
        or "Unknown user"
    )


def _proxy_change_description(request) -> str:
    """Return a readable operation such as ``updated staff profile``."""
    verb = {
        "POST": "submitted",
        "PUT": "updated",
        "PATCH": "updated",
        "DELETE": "deleted",
    }.get(request.method, "changed")
    match = getattr(request, "resolver_match", None)
    raw_name = getattr(match, "url_name", "") or ""
    if raw_name:
        parts = re.split(r"[-_]", raw_name)
    else:
        parts = request.path.strip("/").split("/")
        if parts and re.fullmatch(r"v\d+", parts[0]):
            parts = parts[1:]
    ignored = {"list", "detail", "create", "update", "delete", "destroy"}
    words = [
        part for part in parts
        if (
            part
            and part not in ignored
            and not part.isdigit()
            and not re.fullmatch(r"[0-9a-fA-F-]{16,}", part)
        )
    ]
    resource = " ".join(words) or "record"
    return f"{verb} {resource}"


class TenantContextCleanupMiddleware:
    """Guarantee that request-local tenant state cannot leak between requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        clear_request_context()
        try:
            response = self.get_response(request)
            session = getattr(request, "impersonation_session", None)
            if session is not None:
                _record_proxy_activity(session, request, response)
            # A feature-level event is always more useful than a request-level
            # fallback. Successful reads land in the session's access trail
            # instead of the audit stream; sensitive reads can still emit
            # their own explicit audit event.
            if session is not None and get_current_audit_event_count() == 0:
                from vs_audit.services import emit_audit_event

                actor = getattr(request, "actor_user", None)
                target = getattr(request, "effective_user", None)
                actor_label = _user_label(actor)
                target_label = _user_label(target)
                metadata = {
                    "method": request.method,
                    "path": request.path,
                    "status_code": response.status_code,
                    "fallback_event": True,
                }

                if response.status_code >= 400:
                    denied = response.status_code in {401, 403}
                    outcome = "was blocked" if denied else "failed"
                    emit_audit_event(
                        module_key="PLATFORM",
                        action_type="PROXY_ACTION_FAILED",
                        entity_type="ImpersonationSession",
                        entity_id=str(session.pk),
                        entity_label=target_label,
                        actor_user=actor,
                        effective_user=target,
                        tenant=getattr(request, "tenant", None),
                        impersonation_session=session,
                        severity="WARNING",
                        status="DENIED" if denied else "FAILED",
                        summary=f"{actor_label}'s action {outcome} while proxied as {target_label}",
                        metadata=metadata,
                    )
                elif (
                    request.method not in SAFE_METHODS
                    and request.path not in NON_BUSINESS_PROXY_WRITE_PATHS
                ):
                    change_description = _proxy_change_description(request)
                    metadata["change_description"] = change_description
                    emit_audit_event(
                        module_key="PLATFORM",
                        action_type="PROXY_CHANGE",
                        entity_type="ImpersonationSession",
                        entity_id=str(session.pk),
                        entity_label=target_label,
                        actor_user=actor,
                        effective_user=target,
                        tenant=getattr(request, "tenant", None),
                        impersonation_session=session,
                        summary=(
                            f"{actor_label} {change_description} while proxied as "
                            f"{target_label}"
                        ),
                        metadata=metadata,
                    )
            return response
        finally:
            clear_request_context()
