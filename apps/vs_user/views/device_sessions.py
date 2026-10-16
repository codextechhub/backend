"""Device sessions: refresh, sign out and hand-over (services.device_sessions)."""
from __future__ import annotations

from django.middleware.csrf import get_token
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.response import error_response, success_response

from ..auth_events import AuthEvent
from ..browser_session import (
    clear_device_cookie,
    clear_refresh_cookie,
    device_cookie_value,
    enforce_browser_origin,
    enforce_csrf,
    refresh_cookie_value,
    set_device_cookie,
)
from ..services import device_sessions
from ..services.audit import log_auth_event

__all__ = ["DeviceRefreshView", "DeviceSignOutView", "DeviceHandOverView"]


def _account(request) -> str:
    return str(request.data.get("account") or "").strip()


def _refused(exc, account: str, *, http_status=status.HTTP_401_UNAUTHORIZED):
    response = error_response(
        message=exc.message, error={"error_code": exc.code}, status=http_status,
    )
    if account:
        clear_device_cookie(response, account)
    return response


class _DeviceView(APIView):
    """Credential-cookie routes: no bearer token, CSRF and origin checked."""

    permission_classes = [AllowAny]
    authentication_classes = []
    tenant_param_required = False
    pending_tenant_surface = True


class DeviceRefreshView(_DeviceView):
    """POST /user/auth/device/refresh/

    A new access token for one account on this device. Body ``{account}``;
    the credential is read from that account's own cookie, rotated, and the
    cookie rewritten with the session's lifetime starting again. 401 with
    ``TOKEN_EXPIRED``, ``TOKEN_REVOKED`` or ``TOKEN_INVALID`` clears the
    cookie; the app then drops the account from its list and asks for a
    sign-in.

    docstring-name: Refresh a device session
    """

    def post(self, request):
        enforce_browser_origin(request)
        enforce_csrf(request)
        account = _account(request)
        try:
            session, refresh, access = device_sessions.refresh(
                account, device_cookie_value(request, account),
            )
        except device_sessions.DeviceSessionRefused as exc:
            return _refused(exc, account)
        response = success_response(
            message="Token refreshed successfully.",
            data={"access": access, "account": account, "session_id": session.pk},
        )
        set_device_cookie(
            response, account, refresh, max_age=device_sessions.cookie_max_age(refresh),
        )
        return response


class DeviceSignOutView(_DeviceView):
    """POST /user/auth/device/sign-out/

    Ends one account on this device and clears its cookie. Body ``{account}``.
    Other accounts on the device, and the same account on other devices, stay
    signed in. Always 200: a credential already gone has nothing left to end.

    docstring-name: Sign out of a device session
    """

    def post(self, request):
        enforce_browser_origin(request)
        enforce_csrf(request)
        account = _account(request)
        user = device_sessions.sign_out(account, device_cookie_value(request, account))
        if user is not None:
            log_auth_event(
                actor=user, subject=user, tenant=user.tenant,
                event=AuthEvent.TOKEN_REVOKED, request=request,
            )
        response = success_response(message="Signed out.")
        if account:
            clear_device_cookie(response, account)
        return response


class DeviceHandOverView(_DeviceView):
    """POST /user/auth/device/hand-over/

    Moves a browser session to a device session for the same account, after a
    sign-in on the ordinary page answered ``device_app``. The browser
    credential is revoked and its cookie cleared, and the device session's
    cookie is set. Answers ``{account, access, app_path}``; the page then
    sends the person to ``app_path``. 403 ``DEVICE_SESSION_NOT_OFFERED`` for
    an account with no device app; 401 when the browser session has ended.

    docstring-name: Hand a sign-in over to the device app
    """

    def post(self, request):
        enforce_browser_origin(request)
        enforce_csrf(request)
        try:
            session, refresh, access, policy = device_sessions.hand_over(
                refresh_cookie_value(request), request=request,
            )
        except device_sessions.DeviceSessionRefused as exc:
            http_status = (
                status.HTTP_403_FORBIDDEN
                if exc.code == "DEVICE_SESSION_NOT_OFFERED"
                else status.HTTP_401_UNAUTHORIZED
            )
            return _refused(exc, "", http_status=http_status)
        get_token(request)
        response = success_response(
            message="Signed in on this device.",
            data={
                "account": session.device_key, "access": access,
                "session_id": session.pk, "app_path": policy.app_path,
            },
        )
        clear_refresh_cookie(response)
        set_device_cookie(
            response, session.device_key, refresh,
            max_age=device_sessions.cookie_max_age(refresh),
        )
        return response
