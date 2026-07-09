"""Tiny JSON-over-HTTPS helper built on the stdlib (no third-party ``requests`` dep).  # Shared transport for provider calls.

Kept deliberately small and in one place so the whole network surface of the payment
clients is a single, easily-mocked function (:func:`request_json`). Tests patch this
rather than opening sockets, so the suite never makes a live provider call.  # Centralize HTTP and make it mockable.
"""
from __future__ import annotations  # Defer annotation evaluation for forward references.

import json  # Used to encode requests and decode JSON responses.
import urllib.error  # Standard-library transport errors.
import urllib.request  # Standard-library HTTPS client.

from ..exceptions import ProviderError  # Raised when provider requests fail or return invalid data.

DEFAULT_TIMEOUT = 20  # Conservative timeout so provider calls fail fast.


def request_json(method: str, url: str, *, headers: dict | None = None,  # Define the callable used by this module.
                 body: dict | None = None, provider: str = "",
                 timeout: int = DEFAULT_TIMEOUT) -> dict:  # Start the nested execution block.
    """Make an HTTP request and parse a JSON response, or raise :class:`ProviderError`.

    A non-2xx status, a transport failure, or a non-JSON body all surface as a typed
    ``ProviderError`` (HTTP 502 to our callers) carrying the provider name.
    """
    data = json.dumps(body).encode() if body is not None else None  # Serialize the body once, if present.
    req = urllib.request.Request(url, data=data, method=method.upper())  # Build the HTTP request object.
    req.add_header("Content-Type", "application/json")  # All provider payloads are JSON.
    req.add_header("Accept", "application/json")  # Ask for JSON responses only.
    req.add_header("User-Agent", "CodeX-Finance/1.0 (+https://codexng.com)")  # Identify the client in outbound traffic.
    for key, value in (headers or {}).items():  # Apply provider-specific headers on top of the defaults.
        req.add_header(key, value)  # Preserve each supplied header verbatim.
    try:  # Transport failures should be converted into typed provider errors.
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # Open the request with a bounded timeout.
            payload = resp.read().decode("utf-8")  # Decode the raw response body as UTF-8.
    except urllib.error.HTTPError as exc:  # Provider returned a 4xx/5xx response.
        detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""  # Capture a short error body when available.
        raise ProviderError(  # Raise the domain error for this path.
            f"{provider or 'Provider'} returned HTTP {exc.code}: {detail}",  # Include status and response snippet.
            provider=provider, provider_code=str(exc.code),  # Attach provider metadata for diagnostics.
        )  # Close the grouped expression.
    except urllib.error.URLError as exc:  # DNS, connection, or timeout failure.
        raise ProviderError(  # Raise the domain error for this path.
            f"Could not reach {provider or 'provider'}: {exc.reason}", provider=provider,  # Surface the network reason.
        )  # Close the grouped expression.
    try:  # Successful responses still need to be valid JSON.
        return json.loads(payload) if payload else {}  # Parse JSON, or return an empty dict for empty responses.
    except json.JSONDecodeError:  # Non-JSON bodies are not usable by the provider adapters.
        raise ProviderError(  # Raise the domain error for this path.
            f"{provider or 'Provider'} returned a non-JSON response.", provider=provider,  # Signal a bad upstream response shape.
        )  # Close the grouped expression.
