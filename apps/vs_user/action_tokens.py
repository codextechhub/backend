"""One-time credential generation for account actions.

Raw credentials leave this module only once so they can be placed in an email.
The database stores the keyed SHA-256 digest, scoped to the credential family.
That scope prevents an invitation token from being accepted as a reset token,
even if a caller deliberately submits it to the other endpoint.
"""

from __future__ import annotations

import secrets
import uuid

from django.utils.crypto import salted_hmac


INVITATION_PREFIX = "iv_"
PASSWORD_RESET_PREFIX = "pr_"

_INVITATION_SALT = "vs_user.invitation"
_PASSWORD_RESET_SALT = "vs_user.password_reset"


def _issue(prefix: str, salt: str) -> tuple[str, str]:
    token = f"{prefix}{secrets.token_urlsafe(32)}"
    return token, _digest(token, salt)


def _digest(token: str, salt: str) -> str:
    return salted_hmac(salt, token, algorithm="sha256").hexdigest()


def issue_invitation_token() -> tuple[str, str]:
    """Return a new invitation token and the digest safe to persist."""
    return _issue(INVITATION_PREFIX, _INVITATION_SALT)


def invitation_token_digest(token: str) -> str | None:
    """Return the invitation-family digest, or ``None`` for another family."""
    if not token.startswith(INVITATION_PREFIX):
        # Migration 0010 preserves already-emailed UUID invitations by hashing
        # the old credential before removing it from User. Once those links are
        # used or resent, every newly issued invitation uses the iv_ family.
        try:
            uuid.UUID(token)
        except (TypeError, ValueError, AttributeError):
            return None
    return _digest(token, _INVITATION_SALT)


def issue_password_reset_token() -> tuple[str, str]:
    """Return a new password-reset token and the digest safe to persist."""
    return _issue(PASSWORD_RESET_PREFIX, _PASSWORD_RESET_SALT)


def password_reset_token_digest(token: str) -> str | None:
    """Return the reset-family digest, or ``None`` for another family."""
    if not token.startswith(PASSWORD_RESET_PREFIX):
        return None
    return _digest(token, _PASSWORD_RESET_SALT)
