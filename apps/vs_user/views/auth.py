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
#   SECURITY   - SessionViewSet, AuthAttemptViewSet, AccountLockoutViewSet, AuthEventViewSet

from __future__ import annotations
from uuid import UUID

from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from vs_tenants.models import Tenant
from core.response import success_response, error_response
from ..account_scope import administrable_user
from ..auth_events import AuthEvent
from ..models import (
    User, LoginSession,
)
from ..serializers import (
    ActivationSerializer, ActivationPreviewSerializer, LoginRequestSerializer,
)
from ..services import browser_sessions
from ..services.auth       import LoginService
from ..services.device_sessions import cookie_max_age as device_cookie_max_age
from ..services.invitation import InvitationService
from ..services.audit      import log_auth_event
from ..throttles import CardIdentifierThrottle, CardPreviewIdentifierThrottle
from ..browser_session import (
    clear_refresh_cookie,
    set_device_cookie,
    enforce_browser_origin,
    enforce_csrf,
    is_native_app,
    refresh_cookie_value,
    set_refresh_cookie,
)


# =============================================================================
# # AUTH VIEWS
# =============================================================================

class CsrfCookieView(APIView):
    """Issue the readable double-submit token used by browser auth requests."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        token = get_token(request)
        return success_response(message="CSRF cookie ready.", data={"csrf_token": token})


class LoginView(APIView):
    """
    POST /auth/login/
    Authenticates a user, returns a short-lived access token, and stores the
    rotating refresh credential in an HttpOnly cookie. A store app (a request
    from one of ``NATIVE_APP_ORIGINS``) receives the credential as ``refresh``
    in the response body instead, with no cookie set, because its pages cannot
    hold a cookie for the API; it must ask for ``session: "device"`` and is
    answered 400 ``DEVICE_SESSION_REQUIRED`` otherwise. No other origin ever
    receives ``refresh`` in the body.
    Handles lockout checks, session creation and audit logging - all via
    LoginService.

    The account is named by ``email``, by ``identifier`` (an email address or
    an identifier the tenant issued, typed into one box), or by a platform ``card_id``.
    See LoginRequestSerializer.

    The body may carry an optional ``tenant`` - the slug the frontend reads off
    the subdomain the request came from (a school's page at
    bright-star.xvs.codexng.com sends "bright-star"). When present the tenant is
    resolved first and the account lookup is scoped to it, so an address that
    belongs to a different tenant is refused with the same message a wrong
    password gets. When absent the tenant is derived from the account, as it
    always was. See LoginService.login and services.sign_in_scope.

    Note this is a body key, not the ``?tenant=`` query assertion the
    authenticated endpoints require: there is no token yet to check against.

    ``session: "device"`` asks for a device session instead
    (services.device_sessions): the credential goes into a cookie of its own,
    named for the ``account`` key the response carries, and lives as long as
    the account's product allows (for a store app, in the body, as above). A
    browser sign-in by an account offered a device session answers
    ``device_app`` with the path of its app.

    Permission: AllowAny (public endpoint).

    docstring-name: Log in
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = 'login'
    throttle_classes = [ScopedRateThrottle, CardIdentifierThrottle]

    def post(self, request):
        enforce_browser_origin(request)
        native = is_native_app(request)

        ser = LoginRequestSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)
        if native and ser.validated_data.get('session') != 'device':
            return error_response(
                message="This app must sign in with a device session.",
                error={'code': 'DEVICE_SESSION_REQUIRED'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = LoginService.login(
                email=ser.validated_data['email'],
                password=ser.validated_data['password'],
                tenant=ser.validated_data.get('tenant', ''),
                request=request,
                card_id=ser.validated_data.get('card_id', ''),
                identifier=ser.validated_data.get('identifier', ''),
                device=ser.validated_data.get('session') == 'device',
            )
        except ValueError as e:
            payload = e.args[0] if e.args else {}
            blocked_codes = {
                'ACCOUNT_LOCKED', 'ACCOUNT_SUSPENDED',
                'ACCOUNT_DEACTIVATED', 'ACCOUNT_NOT_ACTIVATED',
                'DEVICE_SESSION_NOT_OFFERED',
            }
            http_status = (
                status.HTTP_403_FORBIDDEN
                if isinstance(payload, dict) and payload.get('code') in blocked_codes
                else status.HTTP_401_UNAUTHORIZED
            )
            message = payload.get('detail', 'Authentication failed.') if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload, status=http_status)

        refresh_token = result.pop('refresh')
        if native:
            result['refresh'] = refresh_token
            return success_response(message="Login successful.", data=result)
        response = success_response(message="Login successful.", data=result)
        # The readable CSRF token is shared with first-party app hosts. The
        # rotating refresh credential remains host-only and outside JavaScript.
        get_token(request)
        if 'account' in result:
            set_device_cookie(
                response, result['account'], refresh_token,
                max_age=device_cookie_max_age(refresh_token),
            )
        else:
            set_refresh_cookie(response, refresh_token)
        return response


class SpecialLoginPreviewView(APIView):
    """
    GET /user/auth/special_login/preview/?card_id=<random UUID>

    ID cards carry a random, revocable lookup key rather than an email address.
    Active platform staff receive a display-name preview. Missing, malformed,
    unknown, stale, locked, suspended, and otherwise inactive identifiers all
    receive the same response, so the endpoint discloses neither an address nor
    an account state. The card and caller IP have independent rate limits.

    Permission: AllowAny - the barcode scanner carries no credentials.

    docstring-name: Barcode login preview
    """

    permission_classes    = [AllowAny]
    authentication_classes = []
    throttle_scope        = 'login_preview'
    throttle_classes      = [ScopedRateThrottle, CardPreviewIdentifierThrottle]

    _UNAVAILABLE_MESSAGE = 'This ID card cannot be used to sign in.'

    def _unavailable(self):
        return error_response(
            message=self._UNAVAILABLE_MESSAGE,
            status=status.HTTP_404_NOT_FOUND,
        )

    def get(self, request):
        raw_identifier = str(request.query_params.get('card_id') or '').strip()
        try:
            identifier = UUID(raw_identifier)
        except (TypeError, ValueError, AttributeError):
            return self._unavailable()

        user = User.objects.filter(
            card_login_id=identifier,
            tenant__kind=Tenant.Kind.PLATFORM,
        ).first()
        if not user or not user.may_sign_in:
            return self._unavailable()

        return success_response(
            message='ID card verified.',
            data={'full_name': user.full_name},
        )


class LogoutView(APIView):
    """
    POST /auth/logout/
    Blacklists the refresh cookie, ending the current session.
    Idempotent - always returns 200 even if the token is already blacklisted.

    The session is authenticated by its refresh cookie and protected by CSRF.
    RBAC: system.session.access.authenticate

    docstring-name: Log out
    """
    permission_classes = [AllowAny]
    # Logs the caller out of their own session - no tenant-scoped input, so
    # ?tenant= is not required.
    tenant_param_required = False
    # Self-scoped, so it stays open to a tenant that has not gone live (FR-012).
    pending_tenant_surface = True

    def post(self, request):
        enforce_csrf(request)

        refresh_token = refresh_cookie_value(request)
        if not refresh_token:
            return error_response(message="Refresh token is required.")

        try:
            token = RefreshToken(refresh_token)
            token_user_id = token.get('user_id')
            if request.user.is_authenticated and str(token_user_id) != str(request.user.id):
                return error_response(message="Token does not belong to the current user.", status=status.HTTP_400_BAD_REQUEST)
            jti = token.get('jti', '')
        except TokenError:
            response = success_response(message="Logged out successfully.")
            clear_refresh_cookie(response)
            return response

        token_user = User.objects.filter(pk=token_user_id).first()
        if token_user is None:
            response = success_response(message="Logged out successfully.")
            clear_refresh_cookie(response)
            return response

        # Scope the logout to THIS session only: blacklist the submitted
        # refresh token and end the session that carries its JTI. Other
        # devices stay logged in - the all-device revocation lives in the
        # admin force-logout / suspend flows (blacklist_all_user_tokens).
        with transaction.atomic():
            try:
                token.blacklist()
            except TokenError:
                pass  # already blacklisted - logout stays idempotent
            LoginSession.objects.filter(
                user=token_user, refresh_jti=str(jti), is_active=True,
            ).update(
                is_active=False,
                ended_at=timezone.now(),
                end_reason='LOGOUT',
            )
            from vs_admin_console.services import end_impersonations_for_user
            end_impersonations_for_user(token_user)

        log_auth_event(
            actor=token_user,
            subject=token_user,
            tenant=token_user.tenant,
            event=AuthEvent.TOKEN_REVOKED,
            request=request,
        )

        response = success_response(message="Logged out successfully.")
        clear_refresh_cookie(response)
        return response


class TokenRefreshView(APIView):
    """
    POST /auth/token/refresh/
    Issues a new access token using the refresh cookie and rotates the cookie.
    Body: {} (the credential is the HttpOnly cookie; a body field is ignored).
    Response: ``access`` and, when the credential belongs to a session,
    ``session_id``; the rotated credential is set as the cookie, never returned.

    The credential the session rotated away from last is still answered for a
    short while (``SESSION_REFRESH_GRACE_SECONDS``) with the credential the
    session is on, so a browser whose last answer was lost recovers by asking
    again with the cookie it holds. An older credential, or that one after the
    window, ends the session and answers 401 ``TOKEN_REVOKED``. Every other
    refusal is a 401 with ``TOKEN_EXPIRED``, ``TOKEN_REVOKED`` or
    ``TOKEN_INVALID``, and clears the cookie. See
    ``services.browser_sessions`` for the rules.

    Permission: AllowAny (public endpoint - token validity is the gate).
    RBAC: identity.access_token.refresh

    docstring-name: Refresh access token
    """
    permission_classes = [AllowAny]
    throttle_scope = "token_refresh"
    throttle_classes = [ScopedRateThrottle]
    # Operates purely on the refresh cookie, so ?tenant= is not required.
    tenant_param_required = False
    pending_tenant_surface = True  # A pending school must be able to stay signed in (FR-012).

    def post(self, request):
        enforce_csrf(request)

        try:
            result = browser_sessions.refresh(refresh_cookie_value(request))
        except browser_sessions.BrowserSessionRefused as exc:
            if exc.reused_by is not None:
                log_auth_event(
                    actor=exc.reused_by, subject=exc.reused_by, tenant=exc.reused_by.tenant,
                    event=AuthEvent.TOKEN_REVOKED, request=request,
                    metadata={"reason": "CREDENTIAL_REUSED", "session_id": exc.session_id},
                )
            response = error_response(
                message=exc.message,
                error={'error_code': exc.code},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            clear_refresh_cookie(response)
            return response

        response_data = {'access': result.access}
        if result.session is not None:
            response_data['session_id'] = result.session.pk
        response = success_response(message="Token refreshed successfully.", data=response_data)
        set_refresh_cookie(response, result.refresh)
        return response


# =============================================================================
# # INVITATION AND ACTIVATION VIEWS
# =============================================================================

class ActivationPreviewView(APIView):
    """
    Called when the user lands on the activation page.
    Returns their name and email so the frontend can pre-fill
    them as read-only fields - the user only needs to set a password.

    Permission: AllowAny (public - user hasn't logged in yet).

    docstring-name: Activation preview
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    # The same budget as accepting the invitation: one link, opened then used.
    throttle_scope = 'activation'
    throttle_classes = [ScopedRateThrottle]

    def get(self, request, token):
        try:
            invitation = InvitationService.get_valid_invitation(token=token)
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
    POST /auth/activate/{token}/
    The person sets their password (``password`` and ``confirm_password``)
    and the account becomes active. Nothing signs them in: no token is issued
    and no cookie is set, so the page sends them on to sign in, to the app
    the activation preview's ``device_app`` names when there is one.

    Permission: AllowAny (public - user hasn't logged in yet).
    RBAC: identity.user_account.activate

    docstring-name: Activate account
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = 'activation'
    # Named here so the limit holds whatever the default throttle classes are.
    throttle_classes = [ScopedRateThrottle]

    def post(self, request, token):
        ser = ActivationSerializer(data=request.data)
        if not ser.is_valid():
            # Carry a code, because the activation screen is pre-authentication
            # and renders only mapped codes and allowlisted sentences - never a
            # raw payload. Without one, the four reasons a password was refused
            # ("too common", "at least 12 characters", "an uppercase letter", "a
            # special character") sat unread in the body while the screen said
            # "Activation failed. Please try again.", which names nothing the
            # person can act on. The password is the only field here that can
            # fail policy, so its failure gets the code the client already maps.
            code = ("PASSWORD_POLICY_VIOLATION" if "password" in ser.errors
                    else "VALIDATION_ERROR")
            # `{code, detail}` is the shape every other refusal uses and the
            # shape the clients read: field errors go UNDER `detail`, not beside
            # the code. Spread flat, they were invisible to the helper that puts
            # a complaint beneath the box it belongs to, so the screen fell back
            # to a sentence that named no requirement at all - which is how
            # "at least 12 characters" reached nobody.
            return error_response(
                message="That password does not meet the requirements.",
                error={"code": code, "detail": ser.errors},
            )

        if ser.validated_data['password'] != ser.validated_data['confirm_password']:
            return error_response(
                message="Passwords do not match.",
                error={'confirm_password': 'Passwords do not match.'},
            )

        try:
            result = InvitationService.activate(
                token=token,
                password=ser.validated_data['password'],
                request=request,
            )
        except ValueError as e:
            payload = e.args[0] if e.args else {}
            # The service writes `message`; this read `detail`, so every
            # sentence it composed - "This invitation link has already been
            # used. Please log in." and its siblings - was thrown away and
            # replaced by the generic line below. `detail` is still honoured
            # for anything that raises in DRF's own shape.
            message = (
                payload.get('message') or payload.get('detail') or 'Activation failed.'
            ) if isinstance(payload, dict) else str(payload)
            return error_response(message=message, error=payload)

        return success_response(message="Account activated successfully.", data=result)


class InvitationResendView(APIView):
    """
    POST /users/{user_id}/invite/resend/
    Resets the 7-day expiry and sends a new invitation email.
    The URL the user receives stays the same -
    vision.codexng.com/invite/{user_id}/ - only the expiry window refreshes.
    Only valid for accounts with status=PENDING.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: identity.user_email.invite

    docstring-name: Resend an invitation
    """
    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.create"

    def post(self, request, user_id):
        user = administrable_user(request, user_id)
        if user is None:
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
