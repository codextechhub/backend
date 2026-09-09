"""Browser refresh-cookie handling and CSRF enforcement."""

from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied


BROWSER_AUTH_MODE = "cookie"


def browser_cookie_mode_requested(request) -> bool:
    """Return whether login explicitly requested the browser-cookie contract."""
    return request.headers.get("X-Auth-Mode", "").strip().lower() == BROWSER_AUTH_MODE


def enforce_browser_origin(request) -> None:
    """Limit browser-cookie login to the configured Console origin.

    The API also serves tenant applications whose origins are CORS-allowed. A
    cookie login is deliberately narrower: otherwise a compromised tenant
    origin could create or drive a Console session through the shared API host.
    Non-browser clients omit Origin and remain usable for operational access.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return

    expected = urlsplit(settings.FRONTEND_BASE_URL)
    supplied = urlsplit(origin)
    if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
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
