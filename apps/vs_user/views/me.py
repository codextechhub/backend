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

    def get(self, request):
        from vs_rbac.evaluator import get_effective_permissions
        permissions = sorted(
            get_effective_permissions(request.user, tenant=getattr(request, "tenant", None))
        )
        return success_response(
            message="Current user retrieved successfully.",
            data={
                "user": UserReadSerializer(request.user).data,
                "tenant": {"slug": request.tenant.slug, "name": request.tenant.name},
                "school": school_public_info(getattr(request.user, "school", None), request),
                "permissions": permissions,
            },
        )


class MySecurityStatsView(APIView):
    """
    GET /user/auth/me/stats/
    Returns security stats scoped to the requesting user — accessible by any
    authenticated user without staff permissions.

    docstring-name: My security stats
    """
    permission_classes = [IsAuthenticatedAndActive]

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

