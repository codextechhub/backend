"""The sign-in account section of every personal data copy (:mod:`core.personal_data`).

Pupils, guardians and staff each hold at most one account (``user``). The
copy shows the account's own details and its sign-in history, never its
password hash, session keys or recovery codes.
"""
from __future__ import annotations

from core.personal_data import KINDS, register, row, rows

ACCOUNT_SKIP = ("password", "is_superuser", "is_staff", "uid", "card_login_id", "branch")
SESSION_SKIP = ("user", "refresh_jti", "previous_refresh_jti", "device_key")
ATTEMPT_SKIP = ("user", "metadata")


def _account(person):
    from .models import AuthAttempt, LoginSession

    user = getattr(person, "user", None)
    if user is None:
        return None
    return {
        "Account": row(user, skip=ACCOUNT_SKIP),
        "Sign-in devices": rows(LoginSession._base_manager.filter(user=user).order_by("-pk"),
                                skip=SESSION_SKIP),
        "Sign-in attempts": rows(AuthAttempt._base_manager.filter(user=user).order_by("-pk"),
                                 skip=ATTEMPT_SKIP),
    }


def register_sections() -> None:
    for kind in KINDS:
        register(kind, "account", "Sign-in account", _account)
