"""Authentication: login, barcode preview, logout, token refresh, activation, invitations.
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
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, ExpiredTokenError, InvalidToken
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.utils import datetime_from_epoch
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from core.response import success_response, error_response
from ..models import (
    User, LoginSession, AuthEventLog,
)
from ..serializers import (
    ActivationSerializer, ActivationPreviewSerializer, LoginRequestSerializer, TokenRefreshSerializer,
)
from ..services.auth       import LoginService
from ..services.invitation import InvitationService
from ..services.audit      import log_auth_event



# =============================================================================
# # AUTH VIEWS
# =============================================================================

class LoginView(APIView):
    """
    POST /auth/login/
    Authenticates a user and returns a JWT token pair.
    Handles lockout checks, school context, session creation,
    and audit logging — all via LoginService.

    Permission: AllowAny (public endpoint).
    RBAC: identity.school_aware_login.enforce

    docstring-name: Log in
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = 'login'

    def post(self, request):
        ser = LoginRequestSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        try:
            result = LoginService.login(
                email=ser.validated_data['email'],
                password=ser.validated_data['password'],
                request=request,
            )
        except ValueError as e:
            payload = e.args[0] if e.args else {}
            blocked_codes = {
                'ACCOUNT_LOCKED', 'ACCOUNT_SUSPENDED',
                'ACCOUNT_DEACTIVATED', 'ACCOUNT_NOT_ACTIVATED',
            }
            http_status = (
                status.HTTP_403_FORBIDDEN
                if isinstance(payload, dict) and payload.get('code') in blocked_codes
                else status.HTTP_401_UNAUTHORIZED
            )
            message = payload.get('detail', 'Authentication failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload, status=http_status)

        return success_response(message="Login successful.", data=result)


class SpecialLoginPreviewView(APIView):
    """
    GET /user/auth/special_login/preview/?email=<email>

    Barcode / ID-card login flow.  The frontend encodes the user's email in the
    QR/barcode and navigates to  /<email>/login.  Before showing the password
    field the page calls this endpoint to:

      1. Confirm the email belongs to a known account.
      2. Return the user's display name (shown in place of the email field).
      3. Surface a clear, status-specific message for non-active accounts so the
         page can inform the user without them having to attempt a full login.

    Responses
    ---------
    200  Active user found → { data: { full_name } }
    403  User exists but account is PENDING / LOCKED / SUSPENDED / DEACTIVATED
    404  No user with that email
    400  email query param missing

    Permission: AllowAny — the barcode scanner carries no credentials.

    docstring-name: Barcode login preview
    """

    permission_classes    = [AllowAny]
    authentication_classes = []
    throttle_scope        = 'login_preview'

    _STATUS_MESSAGES = {
        User.Status.PENDING:     'Account not yet activated. Please check your invitation email or contact your administrator.',
        User.Status.LOCKED:      'Account is locked. Please contact your administrator or reset your password.',
        User.Status.SUSPENDED:   'Account has been suspended. Please contact your administrator.',
        User.Status.DEACTIVATED: 'This account has been deactivated. Please contact your administrator.',
    }

    def get(self, request):
        email = (request.query_params.get('email') or '').strip().lower()
        if not email:
            return error_response(message='email query parameter is required.', status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return error_response(
                message=f'User with {email} does not exist.',
                status=status.HTTP_404_NOT_FOUND,
            )

        if user.status != User.Status.ACTIVE:
            msg = self._STATUS_MESSAGES.get(
                user.status,
                'Account unavailable. Please contact your administrator.',
            )
            return error_response(message=msg, status=status.HTTP_403_FORBIDDEN)

        return success_response(
            message='User found.',
            data={'full_name': user.full_name},
        )


class LogoutView(APIView):
    """
    POST /auth/logout/
    Blacklists the submitted refresh token, ending the current session.
    Idempotent — always returns 200 even if the token is already blacklisted.

    Permission: IsAuthenticated (any logged-in user can log themselves out).
    RBAC: system.session.access.authenticate

    docstring-name: Log out
    """
    permission_classes = [IsAuthenticated]
    # Logs the caller out of their own session — no tenant-scoped input, so
    # ?tenant= is not required.
    tenant_param_required = False

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return error_response(message="Refresh token is required.")

        try:
            token = RefreshToken(refresh_token)
            token_user_id = token.get('user_id')
            if str(token_user_id) != str(request.user.id):
                return error_response(message="Token does not belong to the current user.", status=status.HTTP_400_BAD_REQUEST)
            jti = token.get('jti', '')
        except TokenError:
            return success_response(message="Logged out successfully.")

        # Scope the logout to THIS session only: blacklist the submitted
        # refresh token and end the session that carries its JTI. Other
        # devices stay logged in — the all-device revocation lives in the
        # admin force-logout / suspend flows (blacklist_all_user_tokens).
        with transaction.atomic():
            try:
                token.blacklist()
            except TokenError:
                pass  # already blacklisted — logout stays idempotent
            LoginSession.objects.filter(
                user=request.user, refresh_jti=str(jti), is_active=True,
            ).update(
                is_active=False,
                ended_at=timezone.now(),
                end_reason='LOGOUT',
            )
            from vs_admin_console.services import end_impersonations_for_user
            end_impersonations_for_user(request.user)

        log_auth_event(
            actor=request.user,
            subject=request.user,
            tenant=request.user.tenant,
            event=AuthEventLog.Event.TOKEN_REVOKED,
            request=request,
        )

        return success_response(message="Logged out successfully.")


class TokenRefreshView(APIView):
    """
    POST /auth/token/refresh/
    Issues a new access token using a valid refresh token.

    Permission: AllowAny (public endpoint — token validity is the gate).
    RBAC: identity.access_token.refresh

    docstring-name: Refresh access token
    """
    permission_classes = [AllowAny]
    # Operates purely on the submitted refresh token — token validity is the
    # gate, so ?tenant= is not required (clients send a Bearer header here,
    # which would otherwise trip the mandatory tenant assertion).
    tenant_param_required = False

    def post(self, request):
        ser = TokenRefreshSerializer(data=request.data)

        # SimpleJWT's TokenRefreshSerializer.validate() raises TokenError /
        # InvalidToken when the refresh token is bad — they are not DRF
        # ValidationErrors. Catch them here so the response is a clean 401
        # instead of bubbling up to a 500.
        try:
            ser.is_valid(raise_exception=True)
        except ExpiredTokenError:
            return error_response(
                message="Your session has expired. Please log in again.",
                error={'error_code': 'TOKEN_EXPIRED'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except (TokenError, InvalidToken) as e:
            msg = str(e).lower()
            if 'blacklisted' in msg or 'revoked' in msg:
                return error_response(
                    message="This session has been revoked. Please log in again.",
                    error={'error_code': 'TOKEN_REVOKED'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            if 'expired' in msg:
                return error_response(
                    message="Your session has expired. Please log in again.",
                    error={'error_code': 'TOKEN_EXPIRED'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            return error_response(
                message="Invalid token. Please log in again.",
                error={'error_code': 'TOKEN_INVALID'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except ValidationError:
            # Missing/empty 'refresh' field — treat as invalid.
            return error_response(
                message="Invalid token. Please log in again.",
                error={'error_code': 'TOKEN_INVALID'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        new_refresh_str = ser.validated_data.get('refresh')  # present when ROTATE_REFRESH_TOKENS=True
        response_data = {'access': ser.validated_data['access']}

        if new_refresh_str:
            # Rotation happened: register the new token in OutstandingToken so that
            # blacklist_all_user_tokens() (called on logout/suspend) can reach it.
            try:
                new_refresh = RefreshToken(new_refresh_str)
                jti = new_refresh[jwt_settings.JTI_CLAIM]
                exp = new_refresh['exp']
                user_id = new_refresh[jwt_settings.USER_ID_CLAIM]
                token_user = User.objects.get(pk=user_id)
                OutstandingToken.objects.get_or_create(
                    jti=jti,
                    defaults={
                        'user': token_user,
                        'token': new_refresh_str,
                        'created_at': new_refresh.current_time,
                        'expires_at': datetime_from_epoch(exp),
                    },
                )
                # Keep LoginSession in sync with the new JTI — only the session
                # that owned the OLD token; other devices keep their own JTIs.
                old_jti = ''
                try:
                    old_jti = RefreshToken(
                        request.data.get('refresh', ''), verify=False
                    ).get('jti', '')
                except TokenError:
                    pass
                LoginSession.objects.filter(
                    user=token_user, refresh_jti=str(old_jti), is_active=True,
                ).update(
                    refresh_jti=str(jti),
                    last_seen_at=timezone.now(),
                )
            except (TokenError, User.DoesNotExist):
                # Bookkeeping failed but the new tokens are valid — the client
                # can still use them. Don't fail the whole request.
                pass
            response_data['refresh'] = new_refresh_str

        return success_response(message="Token refreshed successfully.", data=response_data)


# =============================================================================
# # INVITATION AND ACTIVATION VIEWS
# =============================================================================

class ActivationPreviewView(APIView):
    """
    Called when the user lands on the activation page.
    Returns their name and email so the frontend can pre-fill
    them as read-only fields — the user only needs to set a password.

    Permission: AllowAny (public — user hasn't logged in yet).

    docstring-name: Activation preview
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, activation_key):
        try:
            invitation = InvitationService.get_valid_invitation(activation_key=activation_key)
        except ValueError as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('detail', 'Invalid activation key.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload)

        return success_response(
            message="User data retrieved successfully.",
            data=ActivationPreviewSerializer(invitation.user).data,
        )


class ActivationView(APIView):
    """
    POST /auth/activate/{user_id}/
    User submits password + confirm_password.
    On success: account is activated and JWT tokens are returned
    so the user is logged in immediately — no separate login step.

    Permission: AllowAny (public — user hasn't logged in yet).
    RBAC: identity.user_account.activate

    docstring-name: Activate account
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = 'activation'

    def post(self, request, activation_key):
        ser = ActivationSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)

        if ser.validated_data['password'] != ser.validated_data['confirm_password']:
            return error_response(
                message="Passwords do not match.",
                error={'confirm_password': 'Passwords do not match.'},
            )

        try:
            result = InvitationService.activate(
                activation_key=activation_key,
                password=ser.validated_data['password'],
                request=request,
            )
        except ValueError as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('detail', 'Activation failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload)

        return success_response(message="Account activated successfully.", data=result)


class InvitationResendView(APIView):
    """
    POST /users/{user_id}/invite/resend/
    Resets the 7-day expiry and sends a new invitation email.
    The URL the user receives stays the same —
    vision.codexng.com/invite/{user_id}/ — only the expiry window refreshes.
    Only valid for accounts with status=PENDING.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_email.invite

    docstring-name: Resend an invitation
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.create"

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)

        if user.status != User.Status.PENDING:
            return error_response(
                message="Invitations can only be resent for accounts pending activation.",
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            InvitationService.resend(
                user=user,
                requested_by=request.user,
                request=request,
            )
        except Exception as e:
            payload = e.args[0] if e.args else {}
            message = payload.get('detail', 'Resend failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload)

        return success_response(message="Invitation resent successfully.")
