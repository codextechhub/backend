from __future__ import annotations

from .context import clear_current_tenant


class TenantContextCleanupMiddleware:
    """Guarantee that request-local tenant state cannot leak between requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        clear_current_tenant()
        try:
            response = self.get_response(request)
            session = getattr(request, "impersonation_session", None)
            if session is not None:
                from vs_audit.services import emit_audit_event
                emit_audit_event(
                    module_key="PLATFORM",
                    action_type="IMPERSONATED_REQUEST",
                    entity_type="APIRequest",
                    entity_id=request.path,
                    entity_label=f"{request.method} {request.path}",
                    actor_user=getattr(request, "actor_user", None),
                    effective_user=getattr(request, "effective_user", None),
                    tenant=getattr(request, "tenant", None),
                    impersonation_session=session,
                    status="SUCCESS" if response.status_code < 400 else "FAILED",
                    metadata={"method": request.method, "status_code": response.status_code},
                )
            return response
        finally:
            clear_current_tenant()
