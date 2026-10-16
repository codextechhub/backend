"""Long-lived sign-ins kept on one device, several accounts side by side.

A browser session keeps one rotating refresh credential in one cookie, lives a
day, and ends at the next sign-in on that browser. Some accounts are better
served by an installed app that stays signed in for weeks and holds more than
one person: two children sharing a parent's phone, each opening their own
account from the same icon. This module is that second kind of session.

**Which accounts are offered one is the product's decision, not this engine's.**
A product registers a policy from its ``AppConfig.ready()``:
``provider(user) -> DeviceSessionPolicy | None``. The policy names how long the
session lives between uses and where the account's own app is served, and an
account no provider answers for is never given a device session. Staff and
platform accounts therefore keep the browser session they always had.

**One cookie per account, never a token in the page.** Each device session has
a random ``account`` key, and its refresh credential sits in an HttpOnly cookie
named ``AUTH_DEVICE_COOKIE_PREFIX + key``. The app keeps only the key (and a
name to show) to choose between the accounts on the device; a script running in
the page can read neither credential. Refreshing names the key, and the
credential in that cookie must carry the same key, belong to that key's live
session and be the one that session last issued.

**Sliding life.** Every refresh rotates the credential and starts the
policy's lifetime again, so an account used every week never signs out, and one
left alone for longer than the lifetime does. The lifetime is re-read on each
refresh, so a change to it applies from the next one.

**Ending one.** Signing out ends that account on that device only. Ending every
session of an account (a suspension, a deactivation, an administrator's force
logout) already reaches device sessions, because their credentials are
outstanding tokens like any other and their sessions are ``LoginSession`` rows.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from ..models import LoginSession, User
from ..tokens import CodeXRefreshToken
from .audit import blacklist_token_by_jti, get_client_ip, get_device_label

logger = logging.getLogger(__name__)

#: The claim that ties a refresh credential to its device session.
DEVICE_KEY_CLAIM = "dk"


@dataclass(frozen=True)
class DeviceSessionPolicy:
    """How an account's device session behaves.

    ``lifetime`` is how long the session survives without being used.
    ``app_path`` is where the account's own app is served on its tenant's
    address (``/student/``), so a sign-in made elsewhere can send the person
    there.
    """

    lifetime: timedelta
    app_path: str


PolicyProvider = Callable[[User], Optional[DeviceSessionPolicy]]

_providers: dict[str, PolicyProvider] = {}


class DeviceSessionRefused(Exception):
    """A device session that cannot be issued or continued.

    ``code`` is the stable error code; ``message`` the sentence to show.
    ``ends_session`` is False only for ``TOKEN_STALE``: the session is alive
    and its cookie already holds a newer credential, so the refusal must not
    clear it.
    """

    def __init__(self, code: str, message: str, *, ends_session: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.ends_session = ends_session


def _invalid():
    return DeviceSessionRefused("TOKEN_INVALID", "Invalid token. Please sign in again.")


def _revoked():
    return DeviceSessionRefused(
        "TOKEN_REVOKED", "This session has been revoked. Please sign in again.",
    )


def _stale():
    return DeviceSessionRefused(
        "TOKEN_STALE",
        "This sign-in was just renewed in another window. Try again.",
        ends_session=False,
    )


def register_device_session_policy(name: str, provider: PolicyProvider) -> None:
    """Make *provider* decide device sessions for the accounts it knows."""
    _providers[name] = provider


def device_session_policy(user) -> Optional[DeviceSessionPolicy]:
    """The policy the first answering provider gives *user*, or None.

    A provider that raises is logged and treated as not answering, so a broken
    product hook refuses a device session rather than the whole sign-in.
    """
    if user is None or not user.pk:
        return None
    for name, provider in list(_providers.items()):
        try:
            policy = provider(user)
        except Exception:  # noqa: BLE001 - a product hook never breaks sign-in
            logger.exception("Device session policy %r failed.", name)
            continue
        if policy is not None:
            return policy
    return None


def cookie_name(account: str) -> str:
    return f"{settings.AUTH_DEVICE_COOKIE_PREFIX}{account}"


def cookie_max_age(raw: str) -> int:
    """Seconds until the credential *raw* expires, for its cookie's lifetime."""
    exp = RefreshToken(raw, verify=False)["exp"]
    return max(0, int(exp - timezone.now().timestamp()))


def _valid_key(account) -> bool:
    text = str(account or "")
    return 16 <= len(text) <= 64 and text.isalnum()


def _credential(user, account: str, policy: DeviceSessionPolicy) -> CodeXRefreshToken:
    """A refresh credential for *account*, living the policy's lifetime.

    The outstanding-token row is rewritten to the credential's real expiry,
    because the blacklist is flushed by that column: a row expiring at the
    default one day would take the credential's revocation with it while the
    credential itself still had weeks to run.
    """
    token = CodeXRefreshToken.for_user(user)
    token[DEVICE_KEY_CLAIM] = account
    token.set_exp(lifetime=policy.lifetime)
    OutstandingToken.objects.filter(jti=token["jti"]).update(
        token=str(token), expires_at=datetime_from_epoch(token["exp"]),
    )
    return token


def issue(user, *, policy: DeviceSessionPolicy, request=None):
    """Start a device session for *user*; returns ``(session, refresh, access)``."""
    account = secrets.token_hex(16)
    token = _credential(user, account, policy)
    ua_string = request.META.get("HTTP_USER_AGENT", "") if request else ""
    session = LoginSession.objects.create(
        user=user, tenant=user.tenant,
        ip_address=get_client_ip(request), user_agent=ua_string,
        device_label=get_device_label(ua_string, request),
        refresh_jti=str(token["jti"]), last_seen_at=timezone.now(),
        is_active=True, device_key=account,
    )
    return session, str(token), str(token.access_token)


def _read(account: str, raw: str):
    """The signature-checked claims of *raw* and *account*'s live, locked session.

    Being blacklisted is not judged here, because a credential that was
    rotated away a moment ago is blacklisted too, and that is not a revoked
    session: the caller compares the credential with the one the session last
    issued and decides.
    """
    from rest_framework_simplejwt.exceptions import TokenBackendError
    from rest_framework_simplejwt.state import token_backend

    if not _valid_key(account) or not raw:
        raise _invalid()
    try:
        claims = token_backend.decode(raw, verify=True)
    except TokenBackendError as exc:
        if "expired" in str(exc).lower():
            raise DeviceSessionRefused(
                "TOKEN_EXPIRED", "Your session has expired. Please sign in again.",
            ) from None
        raise _invalid() from None
    if claims.get("token_type") != "refresh" or claims.get(DEVICE_KEY_CLAIM) != account:
        raise _invalid()
    session = (
        LoginSession.all_objects.select_for_update()
        .select_related("user__tenant")
        .filter(device_key=account, user_id=claims.get("user_id"), is_active=True)
        .first()
    )
    if session is None:
        raise _revoked()
    return claims, session


def _blacklisted(jti: str) -> bool:
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    return BlacklistedToken.objects.filter(token__jti=jti).exists()


def refresh(account: str, raw: str):
    """Rotate *account*'s credential; returns ``(session, refresh, access)``.

    The session row is locked, so two refreshes racing with one credential
    rotate it once. The one that loses presented the credential the session
    has just rotated away from, and is answered ``TOKEN_STALE``: the session
    lives on, the cookie already holds the new credential, and the caller
    simply asks again. A credential the session still holds but that has been
    blacklisted (every session of the account ended, by a force logout or a
    suspension), or an account no longer allowed in or no longer offered a
    device session, ends the session for good before the refusal is raised.
    """
    with transaction.atomic():
        claims, session = _read(account, raw)
        if session.refresh_jti != str(claims["jti"]):
            raise _stale()
        user = session.user
        policy = device_session_policy(user)
        if user.may_sign_in and policy is not None and not _blacklisted(claims["jti"]):
            RefreshToken(raw).blacklist()
            new = _credential(user, account, policy)
            session.refresh_jti = str(new["jti"])
            session.last_seen_at = timezone.now()
            session.save(update_fields=["refresh_jti", "last_seen_at"])
            return session, str(new), str(new.access_token)
        _end(session, reason="NOT_ALLOWED")
    raise _revoked()


def _end(session, *, reason: str) -> None:
    blacklist_token_by_jti(session.refresh_jti)
    LoginSession.all_objects.filter(pk=session.pk).update(
        is_active=False, ended_at=timezone.now(), end_reason=reason,
    )


@transaction.atomic
def sign_out(account: str, raw: str):
    """End *account* on this device; returns the session's user, or None.

    Only the holder of a credential the session issued can end it, the
    current one or one just rotated away from in another window. A missing,
    expired or forged credential ends nothing, and the caller answers success
    either way and clears the cookie.
    """
    try:
        _token, session = _read(account, raw)
    except DeviceSessionRefused:
        return None
    _end(session, reason="LOGOUT")
    return session.user


@transaction.atomic
def hand_over(raw_browser_token: str, *, request=None):
    """Turn a browser session into a device session for the same account.

    A person signing in on the ordinary sign-in page whose account is offered
    a device session is moved to it without signing in again: the browser
    credential is revoked and its session ended, and a device session is
    issued in its place. Returns ``(session, refresh, access, policy)``.
    """
    try:
        token = RefreshToken(raw_browser_token)
    except TokenError:
        raise DeviceSessionRefused(
            "TOKEN_INVALID", "Your session has ended. Please sign in again.",
        ) from None
    if token.get(DEVICE_KEY_CLAIM):
        raise DeviceSessionRefused(
            "TOKEN_INVALID", "Your session has ended. Please sign in again.",
        )
    user = User.objects.select_related("tenant").filter(pk=token.get("user_id")).first()
    policy = device_session_policy(user)
    if user is None or not user.may_sign_in or policy is None:
        raise DeviceSessionRefused(
            "DEVICE_SESSION_NOT_OFFERED",
            "This app is not available for your account. Use the main sign-in page.",
        )
    token.blacklist()
    LoginSession.all_objects.filter(
        user=user, refresh_jti=str(token["jti"]), is_active=True,
    ).update(is_active=False, ended_at=timezone.now(), end_reason="HANDED_OVER")
    session, refresh_str, access = issue(user, policy=policy, request=request)
    return session, refresh_str, access, policy
