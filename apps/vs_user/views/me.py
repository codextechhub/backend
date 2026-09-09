"""Current-user endpoints: profile, security stats, my password resets.
"""
# views.py
# All views for the vs_users module in one flat file.
#
# Contents (in order):
#   AUTH       - LoginView, LogoutView, TokenRefreshView
#   INVITATION - ActivationPreviewView, ActivationView, InvitationResendView
#   PASSWORD   - PasswordChangeView, PasswordResetRequestView, PasswordResetConfirmView, AdminPasswordResetView
#   USERS      - UserAccountViewSet, UserEmailChangeView, UserSuspendView, UserReactivateView, UserUnlockView
#   SECURITY   - SessionViewSet, AuthAttemptViewSet, AccountLockoutViewSet, AuthEventLogViewSet

from __future__ import annotations
from datetime import timedelta
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView
from vs_rbac.permissions import IsAuthenticatedAndActive
from core.response import success_response
from ..models import (
    AuthAttempt, PasswordResetRequest,
)
from ..serializers import (
    UserReadSerializer,
    school_public_info,
)

from django.utils.dateparse import parse_date as _parse_date



class CurrentUserView(APIView):
    """
    GET /user/auth/me/
    Returns the currently authenticated user's profile and their effective
    permissions. Called by the frontend after a token refresh to keep the
    client-side permission cache in sync with the backend RBAC state.

    docstring-name: Current user profile
    """
    permission_classes = [IsAuthenticatedAndActive]
    # Operates purely on request.user - home tenant is derived from the token,
    # so ?tenant= is not required. request.tenant is still bound by auth.
    tenant_param_required = False
    # Part of the pending-tenant surface (FR-012): the first School Admin of a
    # school that has not gone live must be able to sign in and see themselves.
    pending_tenant_surface = True

    def get(self, request):
        from vs_rbac.evaluator import get_effective_permissions
        from vs_tenants.context import tenant_context_block
        # request.tenant is bound by TenantJWTAuthentication; fall back to the
        # user's home tenant for auth paths that bypass it (e.g. force_authenticate).
        tenant = getattr(request, "tenant", None) or request.user.tenant
        permissions = sorted(
            get_effective_permissions(request.user, tenant=tenant)
        )
        data = {
            "user": UserReadSerializer(request.user).data,
            # Same builder as the login response: the console skips its
            # /me sync straight after a login, so the two must not drift.
            "tenant": tenant_context_block(tenant),
            "school": school_public_info(
                getattr(tenant, "school_profile", None), request, user=request.user,
            ),
            "permissions": permissions,
        }

        # Browser reloads deliberately persist no proxy credential. Tell the
        # actor about their own active session so the client can restore it in
        # memory, then re-run /me with the audited proxy header. The proxied
        # request itself omits this field because its effective context is
        # already established by TenantJWTAuthentication.
        if not request.META.get("HTTP_X_IMPERSONATION_SESSION"):
            from vs_admin_console.models import ImpersonationSession
            from vs_admin_console.serializers import ImpersonationTargetSerializer

            active = (
                ImpersonationSession.objects
                .select_related(
                    "tenant", "target_user__tenant", "target_user__tenant__school_profile",
                )
                .filter(staff_user=request.user, status="ACTIVE")
                .order_by("-started_at", "-pk")
                .first()
            )
            if active is not None:
                data["active_impersonation"] = {
                    "id": active.pk,
                    "tenant_slug": active.tenant.slug,
                    "target": ImpersonationTargetSerializer(active.target_user).data,
                }

        return success_response(
            message="Current user retrieved successfully.",
            data=data,
        )


class MySecurityStatsView(APIView):
    """
    GET /user/auth/me/stats/
    Returns security stats scoped to the requesting user - accessible by any
    authenticated user without staff permissions.

    docstring-name: My security stats
    """
    permission_classes = [IsAuthenticatedAndActive]
    tenant_param_required = False
    pending_tenant_surface = True  # Self-scoped: see FR-012.

    def get(self, request):
        seven_days_ago = timezone.now() - timedelta(days=7)
        failed_7d = AuthAttempt.objects.filter(
            user=request.user,
            created_at__gte=seven_days_ago,
        ).exclude(result=AuthAttempt.Result.SUCCESS).count()
        return success_response(
            message="Security stats retrieved.",
            data={"failed_attempts_7d": failed_7d},
        )


class MyPasswordResetsView(APIView):
    """
    GET /user/auth/me/password-resets/
    Self-service. Returns the current user's full password reset request history,
    newest first. Includes used, expired, and pending requests.

    Permission: IsAuthenticatedAndActive (no RBAC required)

    docstring-name: My password reset history
    """
    permission_classes = [IsAuthenticatedAndActive]
    tenant_param_required = False
    pending_tenant_surface = True  # Self-scoped: see FR-012.

    def get(self, request):
        from ..serializers import MyPasswordResetSerializer
        resets = (
            PasswordResetRequest.objects
            .filter(user=request.user)
            .order_by('-created_at')[:20]
        )
        ser = MyPasswordResetSerializer(resets, many=True)
        return success_response(
            message="Password reset history retrieved.",
            data=ser.data,
        )


def _get_date_param(params, key):
    """Parse a YYYY-MM-DD query param; raise ValidationError with 400 if malformed."""
    raw = params.get(key)
    if not raw:
        return None
    parsed = _parse_date(raw)
    if parsed is None:
        raise ValidationError({key: f'"{raw}" is not a valid date. Use YYYY-MM-DD format.'})
    return parsed
