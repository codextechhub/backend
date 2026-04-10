# views.py
# All views for the vs_users module in one flat file.
#
# Contents (in order):
#   AUTH       - LoginView, LogoutView, TokenRefreshView
#   INVITATION - ActivationPreviewView, ActivationView, InvitationResendView
#   PASSWORD   - PasswordChangeView, PasswordResetRequestView, PasswordResetConfirmView, AdminPasswordResetView
#   USERS      - UserAccountViewSet, UserEmailChangeView, UserSuspendView, UserReactivateView, UserUnlockView
#   SECURITY   - SessionViewSet, AuthAttemptViewSet, AccountLockoutViewSet, AuthEventLogViewSet, SuspiciousLoginEventViewSet

from __future__ import annotations
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import (
    User, UserInvitation, LoginSession, AuthAttempt,
    AccountLockout, AuthEventLog, SuspiciousLoginEvent, PasswordResetRequest,
)
from .serializers import (
    UserReadSerializer, UserListSerializer, UserCreateSerializer,
    UserUpdateSerializer, EmailChangeSerializer,
    ActivationSerializer, ActivationPreviewSerializer,
    LoginRequestSerializer, TokenRefreshSerializer,
    PasswordChangeSerializer, PasswordResetRequestSerializer, PasswordResetConfirmSerializer,
    LoginSessionReadSerializer, ForceLogoutSerializer,
    AuthAttemptReadSerializer, AccountLockoutReadSerializer,
    UnlockAccountSerializer, AuthEventLogReadSerializer,
    SuspiciousLoginEventReadSerializer,
)
from .services.auth       import LoginService
from .services.invitation import InvitationService
from .services.password   import PasswordService
from .services.user       import UserCreationService, EmailChangeService, UserStatusService
from .services.audit      import log_auth_event, blacklist_all_user_tokens, get_client_ip


# =============================================================================
# # AUTH VIEWS
# =============================================================================

class LoginView(APIView):
    """
    POST /auth/login/
    Authenticates a user and returns a JWT token pair.
    Handles lockout checks, school context, session creation,
    and audit logging — all via LoginService.
    """
    permission_classes = [AllowAny]
    throttle_scope = 'login'

    def post(self, request):
        ser = LoginRequestSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = LoginService.login(
                email=ser.validated_data['email'],
                password=ser.validated_data['password'],
                school_slug=ser.validated_data.get('school_slug', ''),
                device_label=ser.validated_data.get('device_label', ''),
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
                if isinstance(payload, dict) and payload.get('error_code') in blocked_codes
                else status.HTTP_401_UNAUTHORIZED
            )
            return Response(payload, status=http_status)

        return Response(result, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """
    POST /auth/logout/
    Blacklists the submitted refresh token, ending the current session.
    Idempotent — always returns 200 even if the token is already blacklisted.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response(
                {'detail': 'Refresh token required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError:
            # Already blacklisted or invalid — still 200, logout is idempotent.
            pass

        log_auth_event(
            actor=request.user,
            subject=request.user,
            school=getattr(request.user, 'school', None),
            event=AuthEventLog.Event.TOKEN_REVOKED,
            request=request,
        )
        return Response({'detail': 'Logged out successfully.'}, status=status.HTTP_200_OK)


class TokenRefreshView(APIView):
    """
    POST /auth/token/refresh/
    Issues a new access token using a valid refresh token.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        ser = TokenRefreshSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            refresh = RefreshToken(ser.validated_data['refresh'])
            return Response(
                {'access': str(refresh.access_token)},
                status=status.HTTP_200_OK,
            )
        except TokenError:
            return Response(
                {'detail': 'Invalid or expired token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

# =============================================================================
# # INVITATION AND ACTIVATION VIEWS
# =============================================================================

class ActivationPreviewView(APIView):
    """
    GET /auth/activate/{user_id}/
    Called when the user lands on the activation page.
    Returns their name and email so the frontend can pre-fill
    them as read-only fields — the user only needs to set a password.
    """
    permission_classes = [AllowAny]

    def get(self, request, user_id):
        try:
            invitation = InvitationService.get_valid_invitation(user_id)
        except ValueError as e:
            return Response(e.args[0], status=status.HTTP_400_BAD_REQUEST)

        return Response(
            ActivationPreviewSerializer(invitation.user).data,
            status=status.HTTP_200_OK,
        )


class ActivationView(APIView):
    """
    POST /auth/activate/{user_id}/
    User submits password + confirm_password.
    On success: account is activated and JWT tokens are returned
    so the user is logged in immediately — no separate login step.
    """
    permission_classes = [AllowAny]

    def post(self, request, user_id):
        ser = ActivationSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        if ser.validated_data['password'] != ser.validated_data['confirm_password']:
            return Response(
                {'confirm_password': 'Passwords do not match.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = InvitationService.activate(
                user_id=user_id,
                password=ser.validated_data['password'],
                request=request,
            )
        except ValueError as e:
            return Response(e.args[0], status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class InvitationResendView(APIView):
    """
    POST /users/{user_id}/invite/resend/
    Resets the 7-day expiry and sends a new invitation email.
    The URL the user receives stays the same —
    vision.codexng.com/invite/{user_id}/ — only the expiry window refreshes.
    Only valid for accounts with status=PENDING.
    """
    permission_classes = [IsAuthenticated, ]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        if user.status != User.Status.PENDING:
            return Response(
                {'detail': 'Invitations can only be resent for accounts pending activation.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            InvitationService.resend(
                user=user,
                requested_by=request.user,
                request=request,
            )
        except Exception as e:
            return Response(
                e.args[0] if e.args else {'detail': 'Resend failed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({'detail': 'Invitation resent.'}, status=status.HTTP_200_OK)

# =============================================================================
# # PASSWORD VIEWS
# =============================================================================

class PasswordChangeView(APIView):
    """
    POST /auth/password/change/
    Logged-in user changes their own password.
    Requires current password for verification.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = PasswordChangeSerializer(data=request.data, context={'request': request})
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            PasswordService.change(
                user=request.user,
                new_password=ser.validated_data['password'],
                request=request,
            )
        except Exception as e:
            return Response(
                e.args[0] if e.args else {},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({'detail': 'Password updated.'}, status=status.HTTP_200_OK)


class PasswordResetRequestView(APIView):
    """
    POST /auth/password/reset/request/
    Self-service reset request.
    Always returns 200 regardless of whether the email exists
    — prevents user enumeration.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        ser = PasswordResetRequestSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        # Service silently does nothing if the email is not found.
        PasswordService.request_reset(
            email=ser.validated_data['email'],
            school_slug=ser.validated_data.get('school_slug', ''),
            request=request,
        )

        return Response(
            {'detail': 'If the account exists, reset instructions have been sent.'},
            status=status.HTTP_200_OK,
        )


class PasswordResetConfirmView(APIView):
    """
    POST /auth/password/reset/confirm/
    Confirms a reset using the token from the email.
    Ends all active sessions on success.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        ser = PasswordResetConfirmSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            PasswordService.confirm_reset(
                raw_token=ser.validated_data['token'],
                new_password=ser.validated_data['password'],
                request=request,
            )
        except ValueError as e:
            return Response(e.args[0], status=status.HTTP_400_BAD_REQUEST)

        return Response({'detail': 'Password reset successful.'}, status=status.HTTP_200_OK)


class AdminPasswordResetView(APIView):
    """
    POST /users/{user_id}/password-reset/
    Admin triggers a 24-hour password reset for a specific user.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, user_id):
        from .models import User
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            PasswordService.admin_reset(
                target_user=user,
                requesting_user=request.user,
                request=request,
            )
        except Exception as e:
            return Response(
                e.args[0] if e.args else {},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response({'detail': 'Password reset email sent.'}, status=status.HTTP_200_OK)

# =============================================================================
# # USER MANAGEMENT VIEWS
# =============================================================================

class UserAccountViewSet(viewsets.ModelViewSet):
    """
    GET    /users/          — list users scoped to requesting admin's school
    POST   /users/          — create user + dispatch invitation email
    GET    /users/{id}/     — retrieve full user profile
    PATCH  /users/{id}/     — update profile fields (not email, not status)
    DELETE /users/{id}/     — soft-deactivate (never hard-delete)
    """

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        if self.action in ('update', 'partial_update'):
            return UserUpdateSerializer
        if self.action == 'list':
            return UserListSerializer
        return UserReadSerializer

    def get_queryset(self):
        user = self.request.user
        qs   = User.objects.select_related('school', 'branch', 'invited_by')
        if getattr(user, 'user_type', None) == User.UserType.VISION_STAFF:
            return qs.all()
        # School Admins see only users within their own school.
        return qs.filter(school=user.school)

    def get_permissions(self):
        if self.action in ('create', 'list'):
            return [IsAuthenticated(), ()]
        if self.action == 'destroy':
            return [IsAuthenticated(), ()]
        if self.action in ('retrieve', 'update', 'partial_update'):
            return [IsAuthenticated(), ()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        UserCreationService.create(
            validated_data=serializer.validated_data,
            requesting_user=self.request.user,
            request=self.request,
        )

    def perform_destroy(self, instance):
        # Never hard-delete. Records and audit history are always preserved.
        UserStatusService.deactivate(
            target_user=instance,
            requesting_user=self.request.user,
            request=self.request,
        )


class UserEmailChangeView(APIView):
    """
    PATCH /users/{user_id}/email/
    Admin-only. Change takes effect immediately and ends all active sessions.
    The user logs in again with the new email and their existing password.
    """
    permission_classes = [IsAuthenticated, ]

    def patch(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        ser = EmailChangeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            updated = EmailChangeService.change_email(
                target_user=user,
                new_email=ser.validated_data['email'],
                requesting_user=request.user,
                request=request,
            )
        except Exception as e:
            detail = e.args[0] if e.args else {}
            http_status = (
                status.HTTP_409_CONFLICT
                if isinstance(detail, dict) and detail.get('error_code') == 'DUPLICATE_EMAIL'
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(detail, status=http_status)

        return Response(UserReadSerializer(updated).data, status=status.HTTP_200_OK)


class UserSuspendView(APIView):
    """POST /users/{user_id}/suspend/"""
    permission_classes = [IsAuthenticated, ]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            updated = UserStatusService.suspend(user, request.user, request)
        except Exception as e:
            return Response(
                e.args[0] if e.args else {},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(UserReadSerializer(updated).data, status=status.HTTP_200_OK)


class UserReactivateView(APIView):
    """POST /users/{user_id}/reactivate/"""
    permission_classes = [IsAuthenticated, ]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            updated = UserStatusService.reactivate(user, request.user, request)
        except Exception as e:
            return Response(
                e.args[0] if e.args else {},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(UserReadSerializer(updated).data, status=status.HTTP_200_OK)


class UserUnlockView(APIView):
    """POST /users/{user_id}/unlock/"""
    permission_classes = [IsAuthenticated, ]

    def post(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            updated = UserStatusService.unlock(user, request.user, request)
        except Exception as e:
            return Response(
                e.args[0] if e.args else {},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(UserReadSerializer(updated).data, status=status.HTTP_200_OK)

# =============================================================================
# # SECURITY AND SESSION VIEWS
# =============================================================================

class SessionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /sessions/
    A user sees only their own sessions.
    Vision Staff see all sessions across the platform.
    """
    serializer_class   = LoginSessionReadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs   = LoginSession.objects.select_related('user', 'school').order_by('-last_seen_at')
        if getattr(user, 'user_type', None) == User.UserType.VISION_STAFF:
            return qs
        return qs.filter(user=user)

    @action(
        detail=False, methods=['post'], url_path='force-logout',
        permission_classes=[IsAuthenticated, ],
    )
    def force_logout(self, request):
        """
        POST /sessions/force-logout/
        Ends sessions for a specific user or a specific session.
        Also blacklists all outstanding JWT tokens for the user.
        """
        ser = ForceLogoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        target_user = ser.validated_data.get('user_id')
        session     = ser.validated_data.get('session_id')
        reason      = ser.validated_data['reason']
        ended       = 0

        if session:
            session.end(reason='FORCE_LOGOUT')
            session.save(update_fields=['is_active', 'ended_at', 'end_reason', 'updated_at'])
            ended = 1

        if target_user:
            sessions = LoginSession.objects.filter(user=target_user, is_active=True)
            ended    = sessions.count()
            for s in sessions:
                s.end(reason='FORCE_LOGOUT')
                s.save(update_fields=['is_active', 'ended_at', 'end_reason', 'updated_at'])
            # Also blacklist all JWT tokens so the user cannot use existing access tokens.
            blacklist_all_user_tokens(target_user)

        log_auth_event(
            actor=request.user,
            subject=target_user if target_user else (session.user if session else None),
            school=getattr(request.user, 'school', None),
            event=AuthEventLog.Event.FORCE_LOGOUT,
            request=request,
            metadata={'ended_sessions': ended, 'reason': reason},
        )
        return Response(
            {'detail': 'Force logout executed.', 'ended_sessions': ended},
            status=status.HTTP_200_OK,
        )


class AuthAttemptViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /auth-attempts/
    Vision Staff only. Shows all login attempts across the platform.
    """
    serializer_class   = AuthAttemptReadSerializer
    permission_classes = [IsAuthenticated, ]
    queryset           = AuthAttempt.objects.select_related('user', 'school').order_by('-created_at')


class AccountLockoutViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /account-lockouts/
    Vision Staff only — lists all locked accounts.

    POST /account-lockouts/unlock/
    School Admins and Vision Staff can unlock accounts.
    Optionally triggers a 24-hour admin password reset email.
    """
    serializer_class   = AccountLockoutReadSerializer
    permission_classes = [IsAuthenticated, ]
    queryset           = AccountLockout.objects.select_related('user').order_by('-updated_at')

    @action(
        detail=False, methods=['post'], url_path='unlock',
        permission_classes=[IsAuthenticated, ],
    )
    def unlock(self, request):
        ser = UnlockAccountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user         = ser.validated_data['user']
        force_reset  = ser.validated_data['force_password_reset']
        reason       = ser.validated_data.get('reason', '')

        # Clear the lockout record.
        lockout, _ = AccountLockout.objects.get_or_create(user=user)
        lockout.clear()
        lockout.save(update_fields=['failure_count', 'locked_until', 'locked_reason', 'updated_at'])

        # Restore user status to ACTIVE if it was LOCKED.
        if user.status == User.Status.LOCKED:
            user.status = User.Status.ACTIVE
            user.save(update_fields=['status', 'updated_at'])

        # Optionally trigger a 24-hour admin password reset.
        if force_reset:
            PasswordService.admin_reset(
                target_user=user,
                requesting_user=request.user,
                request=request,
            )

        log_auth_event(
            actor=request.user,
            subject=user,
            school=getattr(user, 'school', None),
            event=AuthEventLog.Event.ACCOUNT_UNLOCKED,
            request=request,
            metadata={'force_password_reset': force_reset, 'reason': reason},
        )
        return Response({'detail': 'Account unlocked.'}, status=status.HTTP_200_OK)


class AuthEventLogViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /auth-events/
    Vision Staff only. Full audit event log.
    """
    serializer_class   = AuthEventLogReadSerializer
    permission_classes = [IsAuthenticated, ]
    queryset           = AuthEventLog.objects.select_related(
        'actor', 'subject', 'school'
    ).order_by('-created_at')


class SuspiciousLoginEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /suspicious-logins/
    Deferred feature — endpoint kept but not yet active in login flow.
    """
    serializer_class   = SuspiciousLoginEventReadSerializer
    permission_classes = [IsAuthenticated, ]
    queryset           = SuspiciousLoginEvent.objects.select_related('user').order_by('-created_at')