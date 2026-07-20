"""Canonical password policy — the single source of truth for the rules we
enforce on every password-set flow (sign-up, invitation activation, reset,
change) and advertise to clients via ``GET /auth/password/policy/``.

Django's password validators only run where ``validate_password()`` is called
(the password serializers), so the rules are defined once here and reused for
enforcement (``PasswordComplexityValidator``), help text, and the API payload.
The frontend mirrors ``password_policy_payload()`` to render live instructions.
"""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError

#: Minimum length. Keep in sync with the frontend password-policy module.
PASSWORD_MIN_LENGTH = 12

_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def password_requirements() -> list[str]:
    """Human-readable rules in display order — shown to users as instructions."""
    return [
        f"At least {PASSWORD_MIN_LENGTH} characters",
        "An uppercase letter (A–Z)",
        "A lowercase letter (a–z)",
        "A number (0–9)",
        "A special character (e.g. ! @ # $ %)",
    ]


def password_policy_payload() -> dict:
    """Structured policy for ``GET /auth/password/policy/`` — drives client hints."""
    return {
        "min_length": PASSWORD_MIN_LENGTH,
        "require_uppercase": True,
        "require_lowercase": True,
        "require_digit": True,
        "require_special": True,
        "requirements": password_requirements(),
    }


class PasswordComplexityValidator:
    """Enforce the canonical policy: length + upper + lower + digit + special.

    Registered in ``AUTH_PASSWORD_VALIDATORS`` so every ``validate_password()``
    call enforces exactly the rules the UI instructs and the policy endpoint
    advertises. Note: ``create_user()`` / ``set_password()`` do NOT run
    validators — only the password serializers do — so this gates user-chosen
    passwords, not system-seeded test/fixture ones.
    """

    def validate(self, password, user=None):
        errors = []
        if len(password) < PASSWORD_MIN_LENGTH:
            errors.append(
                ValidationError(
                    f"This password must contain at least {PASSWORD_MIN_LENGTH} characters.",
                    code="password_too_short",
                )
            )
        if not re.search(r"[A-Z]", password):
            errors.append(ValidationError("This password must contain an uppercase letter.", code="password_no_upper"))
        if not re.search(r"[a-z]", password):
            errors.append(ValidationError("This password must contain a lowercase letter.", code="password_no_lower"))
        if not re.search(r"[0-9]", password):
            errors.append(ValidationError("This password must contain a number.", code="password_no_digit"))
        if not _SPECIAL_RE.search(password):
            errors.append(ValidationError("This password must contain a special character.", code="password_no_special"))
        if errors:
            raise ValidationError(errors)

    def get_help_text(self) -> str:
        return (
            f"Your password must be at least {PASSWORD_MIN_LENGTH} characters and include "
            "an uppercase letter, a lowercase letter, a number, and a special character."
        )
