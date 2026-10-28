"""Browser sessions: the rotating refresh credential behind the sign-in cookie.

A browser sign-in (the staff app, the platform console, any sign-in that is not
a device session) keeps its refresh credential in an HttpOnly cookie and
exchanges it for a short-lived access token. :func:`refresh` is that exchange:
it validates the credential, rotates it (the one presented is revoked and the
next issued, with the same claims and a new lifetime) and moves the
``LoginSession`` row onto the new credential.

**An answer that never arrives.** Rotation spends the credential before the
browser has the new one, so a connection that drops or a tab that closes
mid-request leaves the cookie holding a credential the session has moved past.
The session therefore remembers the credential it rotated away from last
(``previous_refresh_jti``) and when (``refreshed_at``), and the rules in
``session_credentials`` apply to it exactly as they do to a device session:

* the **current** credential is rotated;
* the **previous** credential, presented within ``SESSION_REFRESH_GRACE_SECONDS``
  of the rotation, is answered with the credential the session is on now (the
  same one every time) and a fresh access token, so a lost answer and two
  windows racing with one credential all end holding the same working cookie.
  If the stored current credential cannot be read, the session rotates instead;
* the previous credential **after** the window is a replay: the session ends
  (``CREDENTIAL_REUSED``), its current credential is revoked, the actor's
  impersonations end as they do on sign-out, and the answer is
  ``TOKEN_REVOKED`` with the account carried in ``reused_by`` for the audit
  trail. A credential two rotations old names no session (only the current and
  previous credentials are recorded), so it is refused as revoked and ends
  nothing.

The grace never outranks the account: an account that may no longer sign in, or
whose current credential has been revoked (a suspension, a deactivation, a
forced logout), ends its session inside the window as it would outside it.

A credential that no ``LoginSession`` row knows about (a session older than the
row, or a path that never made one) is rotated when it is valid, unrevoked and
its account may sign in, and refused otherwise. It has no grace, because there
is no session to remember the credential it rotated away from.

Everything the token library's own refresh checks is checked here: the
signature and expiry, the token type and id, the account existing and passing
``USER_AUTHENTICATION_RULE``, and revocation. A device credential (it carries
the device key) is not a browser credential and is refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework_simplejwt.exceptions import ExpiredTokenError, InvalidToken, TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from ..models import LoginSession, User
from . import session_credentials
from .audit import blacklist_token_by_jti
from .device_sessions import DEVICE_KEY_CLAIM

_EXPIRED = ("TOKEN_EXPIRED", "Your session has expired. Please log in again.")
_REVOKED = ("TOKEN_REVOKED", "This session has been revoked. Please log in again.")
_INVALID = ("TOKEN_INVALID", "Invalid token. Please log in again.")


class BrowserSessionRefused(Exception):
    """A browser credential that cannot be refreshed.

    ``code`` is the stable error code and ``message`` the sentence to show.
    ``reused_by`` and ``session_id`` name the account and session this refusal
    ended because an old credential was presented again, so the caller can
    audit it; both are None for every other refusal.
    """

    def __init__(self, code_and_message, *, reused_by=None, session_id=None):
        code, message = code_and_message
        super().__init__(message)
        self.code = code
        self.message = message
        self.reused_by = reused_by
        self.session_id = session_id


@dataclass(frozen=True)
class BrowserRefresh:
    """The outcome of a refresh: the credential for the cookie, the access token and the session."""

    refresh: str
    access: str
    session: Optional[LoginSession]


class _PresentedRefreshToken(RefreshToken):
    """A refresh token whose revocation is judged by the caller, not on decoding.

    A credential rotated away a moment ago is revoked too, and that is not a
    revoked session: :func:`refresh` compares it with the session's credentials
    and decides. Signature, expiry, id and type are still checked on decoding.
    """

    def check_blacklist(self) -> None:
        return None


def _presented(raw: str) -> _PresentedRefreshToken:
    """The validated token in *raw*, or the refusal that names why it is not one."""
    if not raw:
        raise BrowserSessionRefused(_INVALID)
    try:
        return _PresentedRefreshToken(raw)
    except ExpiredTokenError:
        raise BrowserSessionRefused(_EXPIRED) from None
    except (TokenError, InvalidToken) as exc:
        if "expired" in str(exc).lower():
            raise BrowserSessionRefused(_EXPIRED) from None
        raise BrowserSessionRefused(_INVALID) from None


def _blacklisted(jti: str) -> bool:
    return BlacklistedToken.objects.filter(token__jti=jti).exists()


def _may_continue(user) -> bool:
    """Whether *user* would be allowed to sign in now, by both rules that decide it."""
    return bool(jwt_settings.USER_AUTHENTICATION_RULE(user) and user.may_sign_in)


def refresh(raw: str) -> BrowserRefresh:
    """Rotate the browser credential *raw*; returns the new credential and an access token.

    The session row is locked, so refreshes racing on one session run one after
    another and each sees what the one before it left. The module docstring
    gives the rules; refusals are raised after the transaction commits, so
    ending a session is never rolled back with them.
    """
    token = _presented(raw)
    if token.get(DEVICE_KEY_CLAIM):
        raise BrowserSessionRefused(_INVALID)
    presented = str(token[jwt_settings.JTI_CLAIM])
    user_id = token.payload.get(jwt_settings.USER_ID_CLAIM)
    if not user_id:
        raise BrowserSessionRefused(_INVALID)

    refusal = None
    with transaction.atomic():
        session = (
            LoginSession.all_objects.select_for_update(of=("self",))
            .filter(
                Q(refresh_jti=presented) | Q(previous_refresh_jti=presented),
                user_id=user_id, is_active=True, device_key__isnull=True,
            )
            .first()
        )
        try:
            user = User.objects.select_related("tenant").filter(
                **{jwt_settings.USER_ID_FIELD: user_id},
            ).first()
        except (TypeError, ValueError):
            user = None
        if user is None:
            raise BrowserSessionRefused(_INVALID)
        now = timezone.now()

        if session is None:
            if _blacklisted(presented) or not _may_continue(user):
                refusal = BrowserSessionRefused(_REVOKED)
            else:
                return _rotate(token, None, now)
        else:
            session.user = user
            standing = session_credentials.standing(session, presented, now)
            if standing == session_credentials.STALE:
                _end(session, reason="CREDENTIAL_REUSED")
                refusal = BrowserSessionRefused(
                    _REVOKED, reused_by=user, session_id=session.pk,
                )
            elif not _may_continue(user) or _blacklisted(session.refresh_jti):
                _end(session, reason="NOT_ALLOWED")
                refusal = BrowserSessionRefused(_REVOKED)
            else:
                if standing == session_credentials.PREVIOUS:
                    current = session_credentials.stored_current(session)
                    if current is not None:
                        session.last_seen_at = now
                        session.save(update_fields=["last_seen_at"])
                        return BrowserRefresh(str(current), str(current.access_token), session)
                return _rotate(token, session, now)
    raise refusal


def _rotate(token, session, now) -> BrowserRefresh:
    """Revoke the credential presented, issue the next from its claims and record both.

    The token is rotated as the token library rotates one: a new id, expiry and
    issue time on the same claims, registered as outstanding so a sign-out of
    every session can reach it. When the session is on a different credential
    than the one presented (the presented one was its previous), that current
    credential is revoked as well.
    """
    presented = str(token[jwt_settings.JTI_CLAIM])
    token.blacklist()
    if session is not None and session.refresh_jti != presented:
        blacklist_token_by_jti(session.refresh_jti)
    access = str(token.access_token)
    token.set_jti()
    token.set_exp()
    token.set_iat()
    token.outstand()
    if session is not None:
        session_credentials.record_rotation(
            session, new_jti=str(token[jwt_settings.JTI_CLAIM]),
            previous_jti=presented, now=now,
        )
    return BrowserRefresh(str(token), access, session)


def _end(session, *, reason: str) -> None:
    """End *session*: revoke its credential, close the row and end its owner's impersonations.

    Impersonations end as they do on sign-out, because a proxy session rides on
    the actor's credential and must not outlive it.
    """
    from vs_admin_console.services import end_impersonations_for_user

    blacklist_token_by_jti(session.refresh_jti)
    LoginSession.all_objects.filter(pk=session.pk).update(
        is_active=False, ended_at=timezone.now(), end_reason=reason,
    )
    end_impersonations_for_user(session.user)
