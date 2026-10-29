"""The address a request came from, decided once, where nothing else can be fooled.

Everything that keys on a caller's address reads ``request.META["REMOTE_ADDR"]``.
DRF's per-IP throttles read it directly: ``NUM_PROXIES = 0`` in
``REST_FRAMEWORK`` makes ``get_ident`` return it and never look at
``X-Forwarded-For``. The login history, the lockout counters, the audit trail
and the export download log read it through :func:`get_client_ip`.
:class:`ClientIPMiddleware` is the one place that value is decided.

``X-Forwarded-For`` is never read. Every proxy appends to it, so its leftmost
entries are whatever the caller chose to send. A caller writing a new made-up
address on every request would get a fresh throttle allowance each time and
could put any address it liked into another person's login history.

``settings.CLIENT_IP_HEADERS`` names the ``META`` keys a trusted edge writes the
connecting address into, in order of preference. It is empty unless a settings
module for a deployment behind such an edge sets it:

- Empty (development, tests, CI): ``REMOTE_ADDR`` is the socket's peer and is
  left alone, because nothing sits in front of the server.
- ``staging.py`` (Render): every request passes Cloudflare, then Render's
  router, then gunicorn, so ``REMOTE_ADDR`` is a Render-internal address shared
  by every caller. Cloudflare sets ``CF-Connecting-IP`` and ``True-Client-IP``
  to the address that connected to it, overwriting any value the caller sent,
  so the first of those holding a valid address becomes ``REMOTE_ADDR``.

Cloudflare only overwrites those headers on requests that pass through it. The
Render address of the service can also be reached directly, and a caller going
that way can write any address it likes into them. So when
``settings.CLIENT_IP_EDGE_SECRET`` is set, the headers are trusted only on a
request carrying that secret in ``X-XVS-Edge``, which a Cloudflare Transform
Rule adds to every request it passes on. A request without it keeps Render's
internal address, which every such caller shares, so inventing addresses gains
nothing. The secret is removed from the request once checked, so no log or
error report carries it. With no secret set the headers are trusted as before,
and ``core.W003`` says so on every command.

A header value that does not parse as an IP address is ignored, never copied,
and the next header is tried. If none holds one, ``REMOTE_ADDR`` stays as the
server received it: a wrong but unforgeable address is better than a forgeable
one.
"""

from __future__ import annotations

import hmac
import ipaddress

from django.conf import settings

#: Where the edge's secret arrives: the ``X-XVS-Edge`` request header.
EDGE_SECRET_HEADER = "HTTP_X_XVS_EDGE"


def _valid_ip(value) -> str | None:
    """Return ``value`` as a normalised IP address string, or None if it is not one."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


class ClientIPMiddleware:
    """Write the trusted client address into ``request.META["REMOTE_ADDR"]``.

    First in ``MIDDLEWARE``, so every later middleware, view and throttle sees the
    same answer. The setting is read per request, which keeps it overridable in
    tests.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sent = request.META.pop(EDGE_SECRET_HEADER, "")
        secret = getattr(settings, "CLIENT_IP_EDGE_SECRET", "") or ""
        if secret and not hmac.compare_digest(str(sent), secret):
            # Not from our edge: the headers are whatever the caller wrote.
            return self.get_response(request)
        for key in getattr(settings, "CLIENT_IP_HEADERS", ()) or ():
            address = _valid_ip(request.META.get(key))
            if address:
                request.META["REMOTE_ADDR"] = address
                break
        return self.get_response(request)


def get_client_ip(request) -> str | None:
    """The caller's address as :class:`ClientIPMiddleware` decided it.

    None when there is no request, for service-layer callers acting outside an
    HTTP request.
    """
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None
