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
