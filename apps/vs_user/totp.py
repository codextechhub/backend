"""The six-digit codes an authenticator app shows (RFC 6238, the "TOTP" every app speaks).

A secret is shared once, as a QR code the person scans. From then on the app
and the server each turn the current 30-second step into the same code, so
nothing is sent to the phone and nothing is needed from a network. SHA-1, six
digits and thirty seconds are what Google Authenticator, Microsoft
Authenticator and the rest assume when the QR code says nothing else, so those
are the only parameters offered.

A code is accepted for the step it was made in and the one either side, which
covers a phone clock a little fast or slow and a code typed as it turned over.
The caller keeps the last step it accepted and passes it back, so one code
cannot sign in twice.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30
#: Steps either side of now that are still accepted.
DRIFT = 1


def new_secret() -> str:
    """A fresh 160-bit secret in base32, the form an authenticator app takes."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    """The code for one 30-second step."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def matching_step(secret: str, code: str, *, after: int | None = None, now: float | None = None) -> int | None:
    """The step ``code`` belongs to, or None.

    ``after`` is the last step already accepted for this secret; that step and
    every earlier one are refused, so a code seen over a shoulder or replayed
    from a log is no use once it has been used.
    """
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - DRIFT, step + DRIFT + 1):
        if after is not None and candidate <= after:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` address a QR code carries for the app to scan."""
    label = quote(f"{issuer}:{account}", safe="@:")
    query = urlencode({
        "secret": secret, "issuer": issuer,
        "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD,
    }, quote_via=quote)
    return f"otpauth://totp/{label}?{query}"
