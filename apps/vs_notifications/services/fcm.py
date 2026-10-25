"""Firebase Cloud Messaging: sending one push to one phone.

Both Android and iPhone pushes leave through Firebase Cloud Messaging (the
HTTP v1 API); Firebase relays to Apple, and nothing here talks to APNs. The
sender authenticates with a service-account key whose path is
``FCM_SERVICE_ACCOUNT_FILE``; the Firebase project id is read from the key
itself. With no key, phone push is off (:func:`configured` is False).

**Authentication.** A short-lived RS256 assertion signed with the key's
private key is exchanged at Google's token endpoint for an access token,
which is kept in Django's cache until five minutes before it expires, so a
burst of pushes costs one exchange. The parsed key file is read once per
process.

**Outcomes.** :func:`send` never raises for a refusal. It answers
:attr:`Outcome.DELIVERED` when Firebase accepted the message,
:attr:`Outcome.FORGOTTEN` when Firebase says the token is dead (HTTP 404 or an
``UNREGISTERED`` error code), so the caller can delete the device, and
:attr:`Outcome.FAILED` for anything else, a transport error included, which is
logged and leaves the device alone.

Every network call goes through :func:`_request`, the single seam where tests
substitute Firebase and Google.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("vs_notifications.fcm")

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Seconds a signed assertion is valid for, and how long before an access
#: token's expiry the cached copy is dropped.
ASSERTION_LIFETIME = 60 * 60
TOKEN_EARLY_EXPIRY = 5 * 60
REQUEST_TIMEOUT = 10

_REQUIRED_KEY_FIELDS = ("project_id", "client_email", "private_key")
_KEY_FILES: dict = {}
_KEY_FILES_LOCK = threading.Lock()


class Outcome(enum.Enum):
    """What Firebase did with one message."""

    DELIVERED = "delivered"
    FORGOTTEN = "forgotten"
    FAILED = "failed"


class _AccessTokenError(Exception):
    """Google would not give an access token."""


def _request(method, url, *, headers, body=None, form=None):
    """Make one HTTP call and answer ``(status, parsed_json)``.

    *body* is sent as JSON, *form* as ``application/x-www-form-urlencoded``.
    An error status is an answer, not an exception; a transport failure
    raises. The parsed body is ``{}`` when the response is not JSON.
    """
    data = None
    headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=UTF-8"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def reset_caches() -> None:
    """Forget the parsed key files and the cached access tokens."""
    with _KEY_FILES_LOCK:
        for key_file in _KEY_FILES.values():
            cache.delete(_token_cache_key(key_file))
        _KEY_FILES.clear()


def _service_account() -> dict | None:
    """The parsed key file named by the settings, or None when unusable."""
    path = getattr(settings, "FCM_SERVICE_ACCOUNT_FILE", "")
    if not path:
        return None
    with _KEY_FILES_LOCK:
        known = _KEY_FILES.get(path)
        if known is not None:
            return known
        try:
            with open(path, encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, ValueError):
            logger.warning("The Firebase service-account file %s cannot be read.", path)
            return None
        if not isinstance(parsed, dict) or not all(parsed.get(f) for f in _REQUIRED_KEY_FIELDS):
            logger.warning("The Firebase service-account file %s is missing a field.", path)
            return None
        _KEY_FILES[path] = parsed
        return parsed


def configured() -> bool:
    """True when a readable service-account key is set."""
    return _service_account() is not None


def _token_cache_key(key_file: dict) -> str:
    digest = hashlib.sha256(key_file["client_email"].encode("utf-8")).hexdigest()[:16]
    return f"vs_notifications:fcm:access_token:{digest}"


def _access_token(key_file: dict, *, refresh: bool = False) -> str:
    """A valid access token, from the cache or freshly exchanged."""
    cache_key = _token_cache_key(key_file)
    if not refresh:
        cached = cache.get(cache_key)
        if cached:
            return cached
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": key_file["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
            "iat": now, "exp": now + ASSERTION_LIFETIME,
        },
        key_file["private_key"], algorithm="RS256",
    )
    status, answer = _request(
        "POST", TOKEN_URL, headers={}, form={"grant_type": GRANT_TYPE, "assertion": assertion},
    )
    token = answer.get("access_token")
    if status != 200 or not token:
        raise _AccessTokenError(f"HTTP {status}: {answer.get('error', 'no access token')}")
    lifetime = int(answer.get("expires_in") or 0) - TOKEN_EARLY_EXPIRY
    if lifetime > 0:
        cache.set(cache_key, token, lifetime)
    return token


def _is_dead_token(status: int, answer: dict) -> bool:
    if status == 404:
        return True
    details = (answer.get("error") or {}).get("details") or []
    return any(
        isinstance(item, dict) and item.get("errorCode") == "UNREGISTERED" for item in details
    )


def _message(token, *, title, body, data, tag, ttl_seconds) -> dict:
    return {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {str(key): str(value) for key, value in data.items()},
            "android": {
                "priority": "HIGH",
                "ttl": f"{ttl_seconds}s",
                "notification": {"tag": tag},
            },
            "apns": {
                "headers": {
                    "apns-expiration": str(int(time.time()) + ttl_seconds),
                    "apns-collapse-id": tag[:64],
                },
                "payload": {"aps": {"sound": "default"}},
            },
        },
    }


def send(token, *, title, body, data, tag, ttl_seconds) -> Outcome:
    """Send one message to one phone; see the module docstring for outcomes."""
    key_file = _service_account()
    if key_file is None:
        return Outcome.FAILED
    url = SEND_URL.format(project_id=key_file["project_id"])
    payload = _message(token, title=title, body=body, data=data, tag=tag, ttl_seconds=ttl_seconds)
    try:
        status, answer = _post_message(key_file, url, payload)
    except Exception as exc:  # noqa: BLE001 - a refusal or outage never reaches the caller
        logger.warning("Push to a phone failed: %s", exc)
        return Outcome.FAILED
    if 200 <= status < 300:
        return Outcome.DELIVERED
    if _is_dead_token(status, answer):
        return Outcome.FORGOTTEN
    logger.warning("Firebase refused a push: HTTP %s %s", status, (answer.get("error") or {}).get("status", ""))
    return Outcome.FAILED


def _post_message(key_file: dict, url: str, payload: dict):
    """Post *payload*, fetching a fresh access token once if Firebase rejects the cached one."""
    for refresh in (False, True):
        access_token = _access_token(key_file, refresh=refresh)
        status, answer = _request(
            "POST", url, headers={"Authorization": f"Bearer {access_token}"}, body=payload,
        )
        if status != 401 or refresh:
            return status, answer
        cache.delete(_token_cache_key(key_file))
    return status, answer
