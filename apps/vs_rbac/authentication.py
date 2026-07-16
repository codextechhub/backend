"""JWT authentication with mandatory tenant assertion and audited impersonation."""
from __future__ import annotations

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework_simplejwt.authentication import JWTAuthentication

from vs_tenants.context import set_current_tenant
from vs_tenants.models import Tenant


IMPERSONATION_HEADER = "HTTP_X_IMPERSONATION_SESSION"


class TenantJWTAuthentication(JWTAuthentication):
    def _load_impersonation(self, actor, session_id):
        """Fetch and validate the actor's ACTIVE impersonation session.

        Runs the full session validation (existence, expiry, platform actor,
        target still active) but NOT the tenant-match check — the caller does
        that once the requested tenant is known. Returns the effective (target)
        user or raises AuthenticationFailed.
        """
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
        if impersonation.ends_at is not None and impersonation.ends_at <= timezone.now():
            impersonation.status = "EXPIRED"
            impersonation.ended_at = timezone.now()
            impersonation.save(update_fields=["status", "ended_at"])
            raise AuthenticationFailed("Impersonation session has expired.")
        if getattr(actor.tenant, "kind", None) != Tenant.Kind.PLATFORM:
            raise AuthenticationFailed("Only platform tenant users may impersonate.")
        target = impersonation.target_user
        if not target.is_active or target.status != "ACTIVE":
            impersonation.end()
            raise AuthenticationFailed("The impersonated account is not active.")
        return impersonation, target

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        actor, validated_token = result

        # Reject tokens minted before the tenant upgrade (or before a tenant
        # move): they carry no tenant_slug, or a tenant_id that no longer
        # matches the user's home tenant. Force a fresh sign-in.
        if (not validated_token.get("tenant_slug")
                or str(actor.tenant_id) != str(validated_token.get("tenant_id"))):
            raise AuthenticationFailed("Session predates the tenant upgrade. Sign in again.")

        # DRF runs authentication lazily inside the view's initial(), so the
        # resolved view — and its tenant_param_required /
        # platform_cross_tenant_param flags — is available on the DRF request's
        # parser_context here. (Plain WSGI requests lack it → view is None,
        # which falls back to the required-param defaults.)
        view = (getattr(request, "parser_context", None) or {}).get("view")
        params = getattr(request, "query_params", None)
        if params is None:  # plain WSGIRequest (e.g. in unit tests)
            params = request.GET
        slug = (params.get("tenant") or "").strip().lower()
        session_id = request.META.get(IMPERSONATION_HEADER)

        effective_user = actor
        impersonation = None

        if slug:
            tenant = Tenant.objects.filter(
                slug=slug, status=Tenant.Status.ACTIVE,
            ).first()
            if tenant is None:
                raise NotFound("No tenant matches the requested context.")

            if session_id:
                impersonation, effective_user = self._load_impersonation(actor, session_id)
                if impersonation.tenant_id != tenant.pk:
                    raise NotFound("No tenant matches the requested context.")
            elif actor.tenant_id != tenant.pk:
                # A platform (Codex) actor may assert a different tenant only on
                # views that opt in via platform_cross_tenant_param (e.g. start,
                # list or end impersonation sessions for a school tenant).
                allowed = (
                    getattr(actor.tenant, "kind", None) == Tenant.Kind.PLATFORM
                    and getattr(view, "platform_cross_tenant_param", False)
                )
                if not allowed:
                    raise NotFound("No tenant matches the requested context.")
        else:
            # No ?tenant= provided. Only endpoints that operate purely on
            # request.user may opt out via tenant_param_required = False.
            if getattr(view, "tenant_param_required", True):
                raise ValidationError({"tenant": "A 'tenant' query parameter is required."})
            if session_id:
                impersonation, effective_user = self._load_impersonation(actor, session_id)
                tenant = effective_user.tenant
            else:
                tenant = actor.tenant

        django_request = getattr(request, "_request", request)
        django_request.actor_user = actor
        django_request.effective_user = effective_user
        django_request.impersonation_session = impersonation
        django_request.tenant = tenant
        django_request.rbac_tenant = actor.tenant if impersonation is None else tenant
        set_current_tenant(tenant)
        from vs_tenants.context import set_current_audit_identity
        set_current_audit_identity(
            actor_user=actor,
            effective_user=effective_user,
            impersonation_session=impersonation,
        )
        return effective_user, validated_token
