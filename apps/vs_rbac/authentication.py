"""JWT authentication with mandatory tenant assertion and audited impersonation."""
from __future__ import annotations

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework_simplejwt.authentication import JWTAuthentication

from vs_tenants.context import set_current_tenant
from vs_tenants.models import Tenant


IMPERSONATION_HEADER = "HTTP_X_IMPERSONATION_SESSION"
def _requested_tenant(request):
    slug = (request.query_params.get("tenant") or "").strip().lower()
    if not slug:
        raise ValidationError({"tenant": "A 'tenant' query parameter is required."})
    tenant = Tenant.objects.filter(slug=slug, status=Tenant.Status.ACTIVE).first()
    if tenant is None:
        raise NotFound("No tenant matches the requested context.")
    return tenant


class TenantJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        actor, validated_token = result
        tenant = _requested_tenant(request)
        effective_user = actor
        impersonation = None
        session_id = request.META.get(IMPERSONATION_HEADER)

        if session_id:
            from vs_admin_console.models import ImpersonationSession

            impersonation = (
                ImpersonationSession.objects.select_related(
                    "staff_user__tenant", "target_user__tenant", "tenant",
                )
                .filter(pk=session_id, staff_user=actor, status="ACTIVE")
                .first()
            )
            if impersonation is None:
                raise AuthenticationFailed("Invalid impersonation session.")
            if impersonation.ends_at <= timezone.now():
                impersonation.status = "EXPIRED"
                impersonation.ended_at = timezone.now()
                impersonation.save(update_fields=["status", "ended_at"])
                raise AuthenticationFailed("Impersonation session has expired.")
            if getattr(actor.tenant, "kind", None) != Tenant.Kind.PLATFORM:
                raise AuthenticationFailed("Only platform tenant users may impersonate.")
            if impersonation.tenant_id != tenant.pk:
                raise NotFound("No tenant matches the requested context.")
            effective_user = impersonation.target_user
            if not effective_user.is_active or effective_user.status != "ACTIVE":
                impersonation.end()
                raise AuthenticationFailed("The impersonated account is not active.")
        elif actor.tenant_id != tenant.pk:
            is_impersonation_start = request.path.rstrip("/").endswith("/admin/impersonations/start")
            if not (is_impersonation_start and getattr(actor.tenant, "kind", None) == Tenant.Kind.PLATFORM):
                raise NotFound("No tenant matches the requested context.")

        django_request = getattr(request, "_request", request)
        django_request.actor_user = actor
        django_request.effective_user = effective_user
        django_request.impersonation_session = impersonation
        django_request.tenant = tenant
        django_request.rbac_tenant = actor.tenant if impersonation is None else tenant
        # Internal expand/cutover bridge for domain code that still needs the
        # School profile object. Tenant selection itself is exclusively ?tenant=.
        django_request.school = getattr(tenant, "school_profile", None)
        set_current_tenant(tenant)
        return effective_user, validated_token
