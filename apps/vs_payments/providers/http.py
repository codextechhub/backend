"""Tiny JSON-over-HTTPS helper built on the stdlib (no third-party ``requests`` dep).

Kept deliberately small and in one place so the whole network surface of the payment
clients is a single, easily-mocked function (:func:`request_json`). Tests patch this
rather than opening sockets, so the suite never makes a live provider call.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..exceptions import ProviderError

DEFAULT_TIMEOUT = 20


def request_json(method: str, url: str, *, headers: dict | None = None,
                 body: dict | None = None, provider: str = "",
                 timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Make an HTTP request and parse a JSON response, or raise :class:`ProviderError`.

    A non-2xx status, a transport failure, or a non-JSON body all surface as a typed
    ``ProviderError`` (HTTP 502 to our callers) carrying the provider name.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""
        raise ProviderError(
            f"{provider or 'Provider'} returned HTTP {exc.code}: {detail}",
            provider=provider, provider_code=str(exc.code),
        )
    except urllib.error.URLError as exc:  # DNS / connection / timeout
        raise ProviderError(
            f"Could not reach {provider or 'provider'}: {exc.reason}", provider=provider,
        )
    try:
        return json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        raise ProviderError(
            f"{provider or 'Provider'} returned a non-JSON response.", provider=provider,
        )
