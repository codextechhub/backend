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
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from vs_rbac.permissions import IsAuthenticatedAndActive, IsVisionStaff, HasRBACPermission
from .paginations import UserPagination
from .models import (
    User, UserInvitation, LoginSession, AuthAttempt,
    AccountLockout, AuthEventLog, PasswordResetRequest,
)
from .serializers import (
    PasswordResetPreviewSerializer, UserReadSerializer, UserListSerializer, UserCreateSerializer,
    UserUpdateSerializer, EmailChangeSerializer,
    ActivationSerializer, ActivationPreviewSerializer,
    LoginRequestSerializer, TokenRefreshSerializer,
    PasswordChangeSerializer, PasswordResetRequestSerializer, PasswordResetConfirmSerializer,
    LoginSessionReadSerializer, ForceLogoutSerializer,
    AuthAttemptReadSerializer, AccountLockoutReadSerializer,
    UnlockAccountSerializer, AuthEventLogReadSerializer,
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

    Permission: AllowAny (public endpoint).
    RBAC: identity.school_aware_login.enforce
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

    Permission: IsAuthenticated (any logged-in user can log themselves out).
    RBAC: system.session.access.authenticate
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
            token = RefreshToken(refresh_token)
            blacklist_all_user_tokens(request.user) # Also blacklist all outstanding tokens to ensure complete logout across all sessions.
            jti = token.get('jti', '')
        except TokenError:
            jti = ''

        if jti:
            sessions = LoginSession.objects.filter(
                user=request.user, is_active=True,
            )
            if sessions.exists():
                # Logout is session-based, not only token-based, so we end all active sessions for the user.
                for session in sessions:
                    session.end(reason='LOGOUT')
                    session.save(update_fields=['is_active', 'ended_at', 'end_reason', 'updated_at'])

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

    Permission: AllowAny (public endpoint — token validity is the gate).
    RBAC: identity.access_token.refresh
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
    Called when the user lands on the activation page.
    Returns their name and email so the frontend can pre-fill
    them as read-only fields — the user only needs to set a password.

    Permission: AllowAny (public — user hasn't logged in yet).
    """
    permission_classes = [AllowAny]

    def get(self, request, activation_key):
        try:
            invitation = InvitationService.get_valid_invitation(activation_key=activation_key)
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

    Permission: AllowAny (public — user hasn't logged in yet).
    RBAC: identity.user_account.activate
    """
    permission_classes = [AllowAny]

    def post(self, request, activation_key):
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
                activation_key=activation_key,
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

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_email.invite
    TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.user_email.invite"
          + tenant boundary check (target user must be in actor's school)
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

    Permission: IsAuthenticatedAndActive (any active user can change their own password).
    RBAC: identity.password_policy.enforce
    TODO: Wire up → [IsAuthenticatedAndActive]
    """
    permission_classes = [IsAuthenticatedAndActive]

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

    Permission: AllowAny (public — user may be locked out or forgot password).
    RBAC: identity.user_password.reset
    """
    permission_classes = [AllowAny]

    def post(self, request):
        ser = PasswordResetRequestSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        # Service silently does nothing if the email is not found.
        PasswordService.request_reset(
            email=ser.validated_data['email'],
            request=request,
        )

        return Response(
            {'detail': 'If the account exists, reset instructions have been sent.'},
            status=status.HTTP_200_OK,
        )


class PasswordResetPreviewView(APIView):
    """
    GET /auth/reset-password/{activation_key}/
    Called when the user clicks the link in their email.
    Verifies the token and returns the user's name and email
    so the frontend can pre-fill them as read-only fields.

    Permission: AllowAny (public — user hasn't logged in yet).
    RBAC: identity.user_password.reset
    """
    permission_classes = [AllowAny]

    def get(self, request, activation_key):
        try:
            user = User.objects.get(activation_key=activation_key)
            reset_request = PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).last()
        except PasswordResetRequest.DoesNotExist:
            return Response({'detail': 'Invalid or expired key. Contact your administrator for assistance.'}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'detail': 'Invalid or expired key. Contact your administrator for assistance.'}, status=status.HTTP_400_BAD_REQUEST)

        if reset_request.expires_at < timezone.now():
            return Response({'detail': 'Reset key has expired. Try Again.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            PasswordResetPreviewSerializer(reset_request.user).data,
            status=status.HTTP_200_OK,
        )
    

class PasswordResetConfirmView(APIView):
    """
    POST /auth/password/reset/confirm/
    Confirms a reset using the token from the email.
    Ends all active sessions on success.

    Permission: AllowAny (public — token validity is the gate).
    RBAC: identity.user_password.reset
    """
    permission_classes = [AllowAny]

    def post(self, request, activation_key):
        ser = PasswordResetConfirmSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(activation_key=activation_key)
            if not user:
                return Response({'detail': 'Invalid or expired key. Contact your administrator for assistance.'}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'detail': 'Invalid or expired key. Contact your administrator for assistance.'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            PasswordService.confirm_reset(
                user=user,
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

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_password.reset
    TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.user_password.reset"
          + tenant boundary check (target user must be in actor's school)
    """
    permission_classes = [IsAuthenticatedAndActive]

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

    Permission matrix (TODO — wire up RBAC):
      list:           IsAuthenticatedAndActive, HasRBACPermission
                      RBAC: identity.user_account.create (read access implied by create)
      create:         IsAuthenticatedAndActive, HasRBACPermission
                      RBAC: identity.user_account.create
      retrieve:       IsAuthenticatedAndActive (owner or admin)
                      RBAC: identity.user_account.create (for non-owner access)
      partial_update: IsAuthenticatedAndActive (owner or admin)
                      RBAC: identity.user_account.create (for non-owner access)
      destroy:        IsAuthenticatedAndActive, HasRBACPermission
                      RBAC: identity.user_account.create
                      + must not deactivate self (already enforced in service)
                      + tenant boundary check
    """
    
    rbac_permission = "" # "identity.user_account.view", "identity.user_account.update", "identity.user_account.delete", "identity.user_account.create"
    pagination_class = UserPagination

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        if self.action in ('update', 'partial_update'):
            return UserUpdateSerializer
        if self.action == 'list':
            return UserListSerializer
        return UserReadSerializer

    def get_queryset(self):
        user   = self.request.user
        params = self.request.query_params

        qs = User.objects.select_related('school', 'branch', 'invited_by')

        if getattr(user, 'user_type', None) == User.UserType.VISION_STAFF:
            pass  # no tenant boundary — sees all users
        else:
            qs = qs.filter(school=user.school)

        if status := params.get('status'):
            qs = qs.filter(status=status)

        if user_type := params.get('user_type'):
            qs = qs.filter(user_type=user_type)

        if branch_id := params.get('branch_id'):
            qs = qs.filter(branch_id=branch_id)

        if search := params.get('search'):
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )

        return qs

    def get_permissions(self):
        if self.action in ('create', 'list', 'destroy'):
            return [IsAuthenticatedAndActive(), HasRBACPermission()]
        if self.action in ('retrieve', 'update', 'partial_update'):
            # Owner can view/edit their own profile; others need RBAC.
            return [IsAuthenticatedAndActive(), HasRBACPermission()]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

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
    PATCH /user/{user_id}/email/change/
    Admin-only. Change takes effect immediately and ends all active sessions.
    The user logs in again with the new email and their existing password.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.email_address.verify
    TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.email_address.verify"
          + tenant boundary check (target user must be in actor's school)
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]

    def patch(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        ser = EmailChangeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        if ser.validated_data['email'] == user.email:
            return Response({'detail': 'New email is the same as the current email.'}, status=status.HTTP_400_BAD_REQUEST)

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

        return Response(UserListSerializer(updated).data, status=status.HTTP_200_OK)


class UserSuspendView(APIView):
    """
    POST /users/{user_id}/suspend/

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_account.lock
    TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.user_account.lock"
          + tenant boundary check (target user must be in actor's school)
    """
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
    """
    POST /users/{user_id}/reactivate/

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_account.unlock
    TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.user_account.unlock"
          + tenant boundary check (target user must be in actor's school)
    """
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
    """
    POST /users/{user_id}/unlock/

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_account.unlock
    TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.user_account.unlock"
          + tenant boundary check (target user must be in actor's school)
    """
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

    Permission: IsAuthenticatedAndActive
    RBAC (list own):    system.session.view
    RBAC (list all):    system.session.view (Vision Staff only)
    RBAC (force-logout): system.session.force_logout + identity.user_logout.force
    TODO: Wire up → [IsAuthenticatedAndActive] for list
          Wire up → [IsAuthenticatedAndActive, HasRBACPermission] for force_logout
          with rbac_permission = "system.session.force_logout"
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
        # TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
        #       with rbac_permission = "system.session.force_logout"
        #       + tenant boundary check
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

    Permission: IsAuthenticatedAndActive, IsVisionStaff
    RBAC: identity.authentication_events.log + system.audit.view
    TODO: Wire up → [IsAuthenticatedAndActive, IsVisionStaff]
          or [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "identity.authentication_events.log"
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

    Permission (list):   IsAuthenticatedAndActive, IsVisionStaff
    RBAC (list):         identity.user_account.lock + system.audit.view
    Permission (unlock): IsAuthenticatedAndActive, HasRBACPermission
    RBAC (unlock):       identity.user_account.unlock
    TODO: Wire up → [IsAuthenticatedAndActive, IsVisionStaff] for list
          Wire up → [IsAuthenticatedAndActive, HasRBACPermission] for unlock
          with rbac_permission = "identity.user_account.unlock"
          + tenant boundary check on unlock action
    """
    serializer_class   = AccountLockoutReadSerializer
    permission_classes = [IsAuthenticated, ]
    queryset           = AccountLockout.objects.select_related('user').order_by('-updated_at')

    @action(
        detail=False, methods=['post'], url_path='unlock',
        permission_classes=[IsAuthenticated, ],
        # TODO: Wire up → [IsAuthenticatedAndActive, HasRBACPermission]
        #       with rbac_permission = "identity.user_account.unlock"
        #       + tenant boundary check
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

    Permission: IsAuthenticatedAndActive, IsVisionStaff
    RBAC: system.audit.view + audit.audit_trail_entity.view
    TODO: Wire up → [IsAuthenticatedAndActive, IsVisionStaff]
          or [IsAuthenticatedAndActive, HasRBACPermission]
          with rbac_permission = "system.audit.view"
    """
    serializer_class   = AuthEventLogReadSerializer
    permission_classes = [IsAuthenticated, ]
    queryset           = AuthEventLog.objects.select_related(
        'actor', 'subject', 'school'
    ).order_by('-created_at')


