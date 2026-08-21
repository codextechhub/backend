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
from ..account_scope        import administrable_user
from ..services.password   import PasswordService
from ..password_policy      import password_policy_payload



# =============================================================================
# # PASSWORD VIEWS
# =============================================================================

class PasswordPolicyView(APIView):
    """
    GET /auth/password/policy/
    The canonical password requirements, so every set/change screen can show
    the same instructions the backend actually enforces. Public - the reset and
    activation screens are unauthenticated.

    docstring-name: Password policy
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    tenant_param_required = False
    pending_tenant_surface = True  # Public and self-scoped: see FR-012.

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
    # A pending school's first admin arrives through an invitation and may need
    # to change their password before anything else (FR-012).
    pending_tenant_surface = True

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
    - prevents user enumeration.

    Takes the same optional ``tenant`` body key as login (the slug the frontend
    reads off the subdomain). When present the account is looked up only within
    that tenant, so a reset asked for at one can never rewrite the password of
    an account at another.

    Permission: AllowAny (public - user may be locked out or forgot password).
    RBAC: identity.user_password.reset

    docstring-name: Request password reset
    """
    permission_classes = [AllowAny]
    throttle_scope = 'password_reset'

    def post(self, request):
        ser = PasswordResetRequestSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        # Service silently does nothing if the email is not found, or is not
        # found in the tenant the request named.
        PasswordService.request_reset(
            email=ser.validated_data['email'],
            tenant=ser.validated_data.get('tenant', ''),
            request=request,
        )

        return success_response(message="If the account exists, reset instructions have been sent.")


class PasswordResetPreviewView(APIView):
    """
    GET /auth/reset-password/{activation_key}/
    Called when the user clicks the link in their email.
    Verifies the token and returns the user's name and email
    so the frontend can pre-fill them as read-only fields.

    Permission: AllowAny (public - user hasn't logged in yet).
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

    Permission: AllowAny (public - token validity is the gate).
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

    Refused with 422 when the target's status may not hold a password -
    DEACTIVATED, or one of the never-approved states (DRAFT, PENDING_APPROVAL,
    REJECTED). The check itself lives in ``PasswordService.admin_reset``, not
    here, so it covers every caller of the service and not just this door.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_password.reset

    docstring-name: Admin-initiated password reset
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.update"

    def post(self, request, user_id):
        user = administrable_user(request, user_id)
        if user is None:
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
                # The service speaks 'message'; older payloads here used
                # 'detail'. Read both rather than reporting the generic line
                # over a refusal that named its reason.
                message = raw.get('detail') or raw.get('message') or 'Password reset failed.'
                error_detail = raw
                # A status refusal is not an authorisation failure - the caller
                # holds the key and the account is theirs to administer. It is
                # the same 422 InvitationResendView returns for the same shape
                # of refusal, so the frontend handles one case, not two.
                http_status = (
                    status.HTTP_422_UNPROCESSABLE_ENTITY
                    if raw.get('error_code') == 'ACCOUNT_NOT_ELIGIBLE'
                    else status.HTTP_403_FORBIDDEN
                )
            else:
                message = str(raw) or 'Password reset failed.'
                error_detail = {'detail': message}
                http_status = status.HTTP_403_FORBIDDEN
            return error_response(message=message, error=error_detail, status=http_status)

        return success_response(message="Password reset email sent.")

