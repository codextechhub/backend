"""The rules a session's rotating credential follows, shared by every kind of session.

A session (``LoginSession``) holds one refresh credential at a time, and every
refresh replaces it. Rotation spends the old credential before the client has
the new one, so a connection that drops, a tab that navigates away or an app
killed mid-request leaves the client holding a credential the session has
already moved past. Refusing it would sign the person out days before their
lifetime is up, so the session remembers the credential it rotated away from
last (``previous_refresh_jti``) and when (``refreshed_at``), and that single
credential is still answered for ``SESSION_REFRESH_GRACE_SECONDS``.

Browser sessions (``browser_sessions``) and device sessions
(``device_sessions``) differ in how a credential is issued and carried, and
agree on everything here: how a presented credential stands against its
session, how long the previous one is honoured, and how the answer to a lost
refresh is read back so the client recovers the same credential every time.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

#: The presented credential is the one the session is on.
CURRENT = "current"
#: It is the one the session rotated away from last, inside the grace window.
PREVIOUS = "previous"
#: It is anything else: older than the previous one, or the previous one late.
STALE = "stale"


def refresh_grace() -> timedelta:
    """How long a rotated-away credential is still answered; zero turns it off."""
    seconds = getattr(settings, "SESSION_REFRESH_GRACE_SECONDS", 120)
    return timedelta(seconds=max(0, int(seconds)))


def standing(session, presented_jti: str, now) -> str:
    """Where *presented_jti* stands against *session*: current, previous or stale.

    Only the single previous credential, and only inside the grace window,
    is ``PREVIOUS``. Everything else that is not the current credential is
    ``STALE``: a device that kept an old copy, or somebody who copied it.
    """
    if presented_jti == session.refresh_jti:
        return CURRENT
    if (
        session.previous_refresh_jti
        and presented_jti == session.previous_refresh_jti
        and session.refreshed_at is not None
        and now - session.refreshed_at <= refresh_grace()
    ):
        return PREVIOUS
    return STALE


def stored_current(session):
    """The session's current credential as it was issued, or None when it cannot be read.

    Read from the outstanding-token record the library keeps for every
    credential it issues, so answering a lost refresh stores nothing new. It is
    None when the record is gone, no longer matches the session, or the
    credential is expired or revoked; the caller then rotates instead.
    """
    stored = (
        OutstandingToken.objects.filter(jti=session.refresh_jti)
        .values_list("token", flat=True).first()
    )
    if not stored:
        return None
    try:
        current = RefreshToken(stored)
    except TokenError:
        return None
    if str(current["jti"]) != session.refresh_jti:
        return None
    return current


def record_rotation(session, *, new_jti: str, previous_jti: str, now) -> None:
    """Move *session* onto *new_jti*, remembering *previous_jti* and when."""
    session.refresh_jti = new_jti
    session.previous_refresh_jti = previous_jti
    session.refreshed_at = now
    session.last_seen_at = now
    session.save(update_fields=[
        "refresh_jti", "previous_refresh_jti", "refreshed_at", "last_seen_at",
    ])
