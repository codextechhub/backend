"""Request-local tenant cleanup, and the audit safety net for proxied sessions.

An admin acting as another user must leave a trail even when the view they
reach emits nothing of its own, so this is where a proxied request that
produced no audit event gets a request-level one instead.
"""
from __future__ import annotations

import re

from django.utils import timezone

from .context import clear_request_context, get_current_audit_event_count


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Inbox and read-state upkeep: automatic UI writes, not business changes, so a
# successful call earns no timeline entry. A failed one is still audited.
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
    Never raises - bookkeeping must not break the proxied response.
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
    except Exception:  # pragma: no cover - defensive; see docstring.
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
    """Guarantee that request-local tenant state cannot leak between requests.

    Cleanup runs on the way in and again in ``finally``, so a view that raises
    cannot leave a tenant or a proxy identity behind for the next request the
    same worker serves.

    While a proxy session is active the middleware also closes the gap a
    feature-level event would otherwise leave open. A request that emitted no
    audit event of its own gets a request-level fallback: failures and denials
    as ``PROXY_ACTION_FAILED``, business writes as ``PROXY_CHANGE``. Successful
    reads are deliberately not audited here. They land in the session's access
    trail instead, and a sensitive read still emits its own explicit event.

    A fallback row carries the module of the surface that opened the proxy
    rather than a fixed one, matching the lifecycle bookends in
    ``vs_admin_console``. A school-initiated session writing PLATFORM rows
    would file its own trail behind ``platform.audit.view``, where nobody at
    the school can read it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        clear_request_context()
        try:
            response = self.get_response(request)
            session = getattr(request, "impersonation_session", None)
            if session is not None:
                _record_proxy_activity(session, request, response)
            # Nothing feature-level was emitted: fall back to a request-level row.
            if session is not None and get_current_audit_event_count() == 0:
                from vs_audit.services import emit_audit_event

                actor = getattr(request, "actor_user", None)
                target = getattr(request, "effective_user", None)
                actor_label = _user_label(actor)
                target_label = _user_label(target)
                from vs_admin_console.views import is_platform_actor
                proxy_module_key = "PLATFORM" if is_platform_actor(actor) else "SCHOOL"
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
                        module_key=proxy_module_key,
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
                        module_key=proxy_module_key,
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
