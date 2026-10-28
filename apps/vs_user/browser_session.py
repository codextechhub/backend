"""Browser refresh-cookie handling and CSRF enforcement."""

import re

from django.conf import settings
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied


def enforce_browser_origin(request) -> None:
    """Limit browser login to the configured first-party application origins.

    Exact origins cover the Console and bare development server. Regular
    expressions cover one school slug per production or local hostname.
    Non-browser clients omit Origin and remain usable with a cookie jar.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return

    allowed = set(settings.AUTH_BROWSER_ALLOWED_ORIGINS)
    if origin in allowed:
        return
    if any(re.fullmatch(pattern, origin) for pattern in settings.AUTH_BROWSER_ALLOWED_ORIGIN_REGEXES):
        return
    raise PermissionDenied("Browser authentication is not available from this origin.")


def is_native_app(request) -> bool:
    """Whether *request* comes from one of the store apps' own origins.

    The parents' and pupils' store apps run their pages from inside the phone
    (``capacitor://localhost``, ``https://localhost``), which is a different
    site from the API, so a cookie credential never reaches them. A request
    whose ``Origin`` is exactly one of ``NATIVE_APP_ORIGINS`` is given its
    refresh credential in the response body and sends it back in the body.

    A real browser cannot forge ``Origin``: a website, or a script injected
    into one, always carries its own origin, so it can neither receive a body
    credential nor use one. A non-browser client that sets the header by hand
    already holds the password it had to sign in with, and gains nothing a
    plain API client did not have.
    """
    origin = request.headers.get("Origin")
    return bool(origin) and origin in settings.NATIVE_APP_ORIGINS


def enforce_csrf(request) -> None:
    """Apply Django's CSRF validation to a DRF cookie-authenticated request."""
    check = CSRFCheck(lambda req: None)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        raise PermissionDenied(f"CSRF Failed: {reason}")


def refresh_cookie_value(request) -> str:
    return request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME, "")


def set_refresh_cookie(response, refresh_token: str) -> None:
    """Store the rotating refresh credential outside JavaScript's reach."""
    response.set_cookie(
        key=settings.AUTH_REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.AUTH_REFRESH_COOKIE_MAX_AGE,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response) -> None:
    response.delete_cookie(
        key=settings.AUTH_REFRESH_COOKIE_NAME,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


def device_cookie_value(request, account: str) -> str:
    from .services.device_sessions import cookie_name, valid_key

    if not valid_key(account):
        return ""
    return request.COOKIES.get(cookie_name(account), "")


def set_device_cookie(response, account: str, refresh_token: str, *, max_age: int) -> None:
    """Store one device session's credential in a cookie of its own.

    Sent only to the sign-in routes, and as tightly as the browser refresh
    cookie otherwise.
    """
    from .services.device_sessions import cookie_name

    response.set_cookie(
        key=cookie_name(account),
        value=refresh_token,
        max_age=max_age,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
        path=settings.AUTH_DEVICE_COOKIE_PATH,
    )


def clear_device_cookie(response, account: str) -> None:
    """Clear one device session's cookie.

    An account that is not a key this server issues names no cookie it ever
    set, and may make an illegal cookie name, so there is nothing to clear.
    """
    from .services.device_sessions import cookie_name, valid_key

    if not valid_key(account):
        return
    response.delete_cookie(
        key=cookie_name(account),
        path=settings.AUTH_DEVICE_COOKIE_PATH,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )
