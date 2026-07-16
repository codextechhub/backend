from __future__ import annotations

from .context import clear_request_context, get_current_audit_event_count


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _user_label(user) -> str:
    if user is None:
        return "Unknown user"
    return (
        getattr(user, "full_name", None)
        or getattr(user, "get_full_name", lambda: "")()
        or getattr(user, "email", None)
        or "Unknown user"
    )


class TenantContextCleanupMiddleware:
    """Guarantee that request-local tenant state cannot leak between requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        clear_request_context()
        try:
            response = self.get_response(request)
            session = getattr(request, "impersonation_session", None)
            # A feature-level event is always more useful than a request-level
            # fallback. Successful reads are intentionally quiet; sensitive
            # reads can still emit their own explicit audit event.
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
                elif request.method not in SAFE_METHODS:
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
                        summary=f"{actor_label} made a change while proxied as {target_label}",
                        metadata=metadata,
                    )
            return response
        finally:
            clear_request_context()
