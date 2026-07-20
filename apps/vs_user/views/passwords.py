"""Password change/reset flows.
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
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from core.response import success_response, error_response
from ..models import (
    User, PasswordResetRequest,
)
from ..serializers import (
    PasswordResetPreviewSerializer, PasswordChangeSerializer, PasswordResetRequestSerializer, PasswordResetConfirmSerializer,
)
from ..services.password   import PasswordService
from ..password_policy      import password_policy_payload



# =============================================================================
# # PASSWORD VIEWS
# =============================================================================

class PasswordPolicyView(APIView):
    """
    GET /auth/password/policy/
    The canonical password requirements, so every set/change screen can show
    the same instructions the backend actually enforces. Public — the reset and
    activation screens are unauthenticated.

    docstring-name: Password policy
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    tenant_param_required = False

    def get(self, request):
        return success_response("Password policy.", password_policy_payload())

class PasswordChangeView(APIView):
    """
    POST /auth/password/change/
    Logged-in user changes their own password.
    Requires current password for verification.

    Permission: IsAuthenticatedAndActive (any active user can change their own password).
    RBAC: identity.password_policy.enforce
    TODO: Wire up → [IsAuthenticatedAndActive]

    docstring-name: Change password
    """
    permission_classes = [IsAuthenticatedAndActive]
    # Self-service: changes only request.user's own password with no
    # tenant-scoped input, so ?tenant= is not required.
    tenant_param_required = False

    def post(self, request):
        ser = PasswordChangeSerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        try:
            PasswordService.change(
                user=request.user,
                new_password=ser.validated_data['password'],
                request=request,
            )
        except Exception as e:
            raw = e.args[0] if e.args else {}
            if isinstance(raw, dict):
                message = raw.get('detail', 'Password change failed.')
                error_detail = raw
            else:
                message = str(raw) or 'Password change failed.'
                error_detail = {'detail': message}
            return error_response(message=message, error=error_detail)

        return success_response(message="Password updated successfully.")


class PasswordResetRequestView(APIView):
    """
    POST /auth/password/reset/request/
    Self-service reset request.
    Always returns 200 regardless of whether the email exists
    — prevents user enumeration.

    Permission: AllowAny (public — user may be locked out or forgot password).
    RBAC: identity.user_password.reset

    docstring-name: Request password reset
    """
    permission_classes = [AllowAny]
    throttle_scope = 'password_reset'

    def post(self, request):
        ser = PasswordResetRequestSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        # Service silently does nothing if the email is not found.
        PasswordService.request_reset(
            email=ser.validated_data['email'],
            request=request,
        )

        return success_response(message="If the account exists, reset instructions have been sent.")


class PasswordResetPreviewView(APIView):
    """
    GET /auth/reset-password/{activation_key}/
    Called when the user clicks the link in their email.
    Verifies the token and returns the user's name and email
    so the frontend can pre-fill them as read-only fields.

    Permission: AllowAny (public — user hasn't logged in yet).
    RBAC: identity.user_password.reset

    docstring-name: Password reset preview
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, activation_key):
        try:
            user = User.objects.get(activation_key=activation_key)
        except User.DoesNotExist:
            return error_response(message="Invalid or expired key. Contact your administrator for assistance.")

        reset_request = PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).last()
        if not reset_request:
            return error_response(message="Invalid or expired key. Contact your administrator for assistance.")

        if reset_request.expires_at < timezone.now():
            return error_response(message="Reset key has expired. Try again.")

        return success_response(
            message="User data retrieved successfully.",
            data=PasswordResetPreviewSerializer(reset_request.user).data,
        )


class PasswordResetConfirmView(APIView):
    """
    POST /auth/password/reset/confirm/
    Confirms a reset using the token from the email.
    Ends all active sessions on success.

    Permission: AllowAny (public — token validity is the gate).
    RBAC: identity.user_password.reset

    docstring-name: Confirm password reset
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, activation_key):
        ser = PasswordResetConfirmSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        try:
            user = User.objects.get(activation_key=activation_key)
        except User.DoesNotExist:
            return error_response(message="Invalid or expired key. Contact your administrator for assistance.")

        try:
            PasswordService.confirm_reset(
                user=user,
                new_password=ser.validated_data['password'],
                request=request,
            )
        except ValueError as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('message', payload.get('detail', 'Password reset failed.')) if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload)

        return success_response(message="Password reset successful.")


class AdminPasswordResetView(APIView):
    """
    POST /{user_id}/password-reset/
    Admin triggers a 24-hour password reset for a specific user.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_password.reset

    docstring-name: Admin-initiated password reset
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.update"

    def post(self, request, user_id):
        from ..models import User
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)

        try:
            PasswordService.admin_reset(
                target_user=user,
                requesting_user=request.user,
                request=request,
            )
        except Exception as e:
            raw = e.args[0] if e.args else {}
            if isinstance(raw, dict):
                message = raw.get('detail', 'Password reset failed.')
                error_detail = raw
            else:
                message = str(raw) or 'Password reset failed.'
                error_detail = {'detail': message}
            return error_response(message=message, error=error_detail, status=status.HTTP_403_FORBIDDEN)

        return success_response(message="Password reset email sent.")

