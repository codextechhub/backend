"""Device sessions: refresh, sign out and hand-over (services.device_sessions).

A browser keeps each account's credential in a cookie and proves itself with
CSRF. A store app (a request from one of ``NATIVE_APP_ORIGINS``) keeps it in
the phone's keychain and sends it in the request body as ``refresh``; the
response carries the rotated one the same way and no cookie is read, set or
cleared. CSRF protects cookie credentials, and a body credential cannot be
sent by a forging site because it cannot read it. From every other origin a
``refresh`` key in the body is ignored.
"""
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
    is_native_app,
    refresh_cookie_value,
    set_device_cookie,
)
from ..services import device_sessions
from ..services.audit import log_auth_event

__all__ = ["DeviceRefreshView", "DeviceSignOutView", "DeviceHandOverView"]


def _account(request) -> str:
    return str(request.data.get("account") or "").strip()


def _credential(request, account: str, *, native: bool) -> str:
    """The credential the request presents: its body for a store app, else the account's cookie."""
    if native:
        value = request.data.get("refresh")
        return value if isinstance(value, str) else ""
    return device_cookie_value(request, account)


def _refused(exc, account: str, *, http_status=status.HTTP_401_UNAUTHORIZED, native: bool = False):
    """The refusal, clearing the account's cookie.

    A store app holds no cookie, so nothing is cleared for it.
    """
    response = error_response(
        message=exc.message, error={"error_code": exc.code},
        status=http_status,
    )
    if account and not native:
        clear_device_cookie(response, account)
    return response


class _DeviceView(APIView):
    """Credential routes: no bearer token; origin checked, and CSRF for cookie credentials."""

    permission_classes = [AllowAny]
    authentication_classes = []
    tenant_param_required = False
    pending_tenant_surface = True


class DeviceRefreshView(_DeviceView):
    """POST /user/auth/device/refresh/

    A new access token for one account on this device. Body ``{account}``;
    the credential is read from that account's own cookie, rotated, and the
    cookie rewritten with the session's lifetime starting again. A store app
    sends the credential as ``refresh`` in the body instead, without CSRF, and
    receives the rotated one as ``refresh`` in the response; no cookie is read
    or set. 401 with ``TOKEN_EXPIRED``, ``TOKEN_REVOKED`` or ``TOKEN_INVALID``
    clears the cookie; the app then drops the account (and, for a store app,
    its stored credential) from its list and asks for a sign-in.

    The credential the session rotated away from last is still answered for a
    short while (``DEVICE_SESSION_REFRESH_GRACE_SECONDS``) with the credential
    the session is on, so a device whose last answer was lost recovers by
    asking again with what it holds. An older credential, or that one after
    the window, ends the session and answers 401 ``TOKEN_REVOKED``. No
    refresh answers 409: ``TOKEN_STALE`` is retired, and an app that still
    retries on it simply never sees it.

    docstring-name: Refresh a device session
    """

    def post(self, request):
        enforce_browser_origin(request)
        native = is_native_app(request)
        if not native:
            enforce_csrf(request)
        account = _account(request)
        try:
            session, refresh, access = device_sessions.refresh(
                account, _credential(request, account, native=native),
            )
        except device_sessions.DeviceSessionRefused as exc:
            if exc.reused_by is not None:
                log_auth_event(
                    actor=exc.reused_by, subject=exc.reused_by, tenant=exc.reused_by.tenant,
                    event=AuthEvent.TOKEN_REVOKED, request=request,
                    metadata={"reason": "CREDENTIAL_REUSED", "device_key": account},
                )
            return _refused(exc, account, native=native)
        data = {"access": access, "account": account, "session_id": session.pk}
        if native:
            data["refresh"] = refresh
            return success_response(message="Token refreshed successfully.", data=data)
        response = success_response(message="Token refreshed successfully.", data=data)
        set_device_cookie(
            response, account, refresh, max_age=device_sessions.cookie_max_age(refresh),
        )
        return response


class DeviceSignOutView(_DeviceView):
    """POST /user/auth/device/sign-out/

    Ends one account on this device and clears its cookie. Body ``{account}``;
    a store app also sends its credential as ``refresh`` and has no cookie to
    clear.
    Other accounts on the device, and the same account on other devices, stay
    signed in. Always 200: a credential already gone has nothing left to end.

    docstring-name: Sign out of a device session
    """

    def post(self, request):
        enforce_browser_origin(request)
        native = is_native_app(request)
        if not native:
            enforce_csrf(request)
        account = _account(request)
        user = device_sessions.sign_out(account, _credential(request, account, native=native))
        if user is not None:
            log_auth_event(
                actor=user, subject=user, tenant=user.tenant,
                event=AuthEvent.TOKEN_REVOKED, request=request,
            )
        response = success_response(message="Signed out.")
        if account and not native:
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
