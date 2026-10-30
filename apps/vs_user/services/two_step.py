"""Two-step sign-in: a code from an authenticator app after the password.

**Who is asked.** CodeX staff always: a console account reaches every school.
At a school, the people who can run it or move its money, while the school's
``identity.two_step.required`` setting is on, which it is unless the school
turns it off:

* administrators: anyone holding a ``school.administrators.``,
  ``school.roles.`` or ``platform.team.`` permission, or
  ``school.settings.update``. School and branch administrators both hold one;
  a teacher holds none.
* finance: anyone holding a ``finance.`` or ``payments.`` permission that does
  more than view.

The test reads permissions, not role names, because a role's name is whatever
the person who created it typed.

**The step between.** A correct password from someone who is asked opens a
challenge (``TwoStepChallenge``) and signs nobody in: no token, no cookie, no
session exists until the code is right. A person with no authenticator yet is
sent to set one up with that challenge, scanning a QR code and typing back its
first code, and is handed ten backup codes. A wrong code counts towards the
same lockout as a wrong password, and a challenge dies after five of them or
ten minutes.

**A lost phone.** A backup code stands in for the app once. An administrator
can reset someone else's two-step sign-in, never their own, and that person
sets up again at their next sign-in.

The secret is stored as the app holds it, as Django's own one-time-password
packages do; a backup code is kept only as a digest.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .. import totp
from ..action_tokens import (
    issue_two_step_challenge_token,
    two_step_challenge_digest,
    two_step_recovery_digest,
)
from ..auth_events import AuthEvent
from ..models import TwoStepAuthenticator, TwoStepChallenge, TwoStepRecoveryCode
from .audit import log_auth_event

SETTING_KEY = "identity.two_step.required"
CHALLENGE_LIFETIME = timedelta(minutes=10)
CHALLENGE_ATTEMPTS = 5
RECOVERY_CODE_COUNT = 10

ADMIN_PREFIXES = ("school.administrators.", "school.roles.", "platform.team.")
ADMIN_KEYS = frozenset({"school.settings.update"})
MONEY_PREFIXES = ("finance.", "payments.")

#: What the sign-in page is told, by the code it is answered with.
ASK_FOR_CODE = "TWO_STEP_REQUIRED"
ASK_TO_SET_UP = "TWO_STEP_SET_UP_REQUIRED"


class TwoStepRefused(Exception):
    """A second step that cannot go on, with the code and words the client shows."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def runs_or_pays(permissions) -> bool:
    """Whether a set of permission keys can administer a school or move its money."""
    for key in permissions:
        if key in ADMIN_KEYS or key.startswith(ADMIN_PREFIXES):
            return True
        if key.startswith(MONEY_PREFIXES) and not key.endswith(".view"):
            return True
    return False


def tenant_requires(tenant) -> bool:
    """The school's setting; on unless the school has turned it off."""
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value

    definition = ConfigurationDefinition.objects.filter(key=SETTING_KEY).first()
    if definition is None:
        return True
    value, _ = resolve_value(definition, tenant=tenant)
    return value is not False


def required_for(user) -> bool:
    """Whether this person must give a code to sign in."""
    if user.is_platform_user:
        return True
    if not tenant_requires(user.tenant):
        return False
    from vs_rbac.evaluator import get_effective_permissions

    return runs_or_pays(get_effective_permissions(user, tenant=user.tenant))


def is_set_up(user) -> bool:
    return TwoStepAuthenticator.objects.filter(user=user, confirmed_at__isnull=False).exists()


def open_challenge(user, *, device: bool, email_entered: str) -> dict:
    """Open the step between password and code; the payload the sign-in answers with."""
    purpose = (
        TwoStepChallenge.Purpose.VERIFY if is_set_up(user) else TwoStepChallenge.Purpose.SET_UP
    )
    token, digest = issue_two_step_challenge_token()
    TwoStepChallenge.objects.create(
        user=user, token_hash=digest, purpose=purpose, device=device,
        email_entered=email_entered or '',
        expires_at=timezone.now() + CHALLENGE_LIFETIME,
    )
    if purpose == TwoStepChallenge.Purpose.VERIFY:
        code, detail = ASK_FOR_CODE, "Enter the 6-digit code from your authenticator app."
    else:
        code, detail = ASK_TO_SET_UP, "Your account needs two-step sign-in. Set up an authenticator app to continue."
    return {
        "code": code,
        "detail": detail,
        "challenge": token,
        "expires_in": int(CHALLENGE_LIFETIME.total_seconds()),
    }


def _issuer(user) -> str:
    """The name the authenticator app lists the account under."""
    if user.is_platform_user:
        return "CodeX"
    school = getattr(user.tenant, "school_profile", None)
    return (school.name if school else user.tenant.name) or "XVS"


def _live_challenge(token: str, purpose: str) -> TwoStepChallenge:
    """The open challenge for ``token``, locked for this transaction."""
    digest = two_step_challenge_digest(token or "")
    challenge = None
    if digest:
        challenge = (
            TwoStepChallenge.objects.select_for_update().select_related("user", "user__tenant")
            .filter(token_hash=digest).first()
        )
    if (
        challenge is None or challenge.used_at is not None
        or challenge.expires_at <= timezone.now() or challenge.purpose != purpose
    ):
        raise TwoStepRefused(
            "TWO_STEP_CHALLENGE_INVALID",
            "This sign-in has expired. Enter your email and password again.",
        )
    return challenge


def start_set_up(token: str) -> dict:
    """A new secret for the person signing in, as a QR code address and as text."""
    with transaction.atomic():
        challenge = _live_challenge(token, TwoStepChallenge.Purpose.SET_UP)
        user = challenge.user
        secret = totp.new_secret()
        TwoStepAuthenticator.objects.update_or_create(
            user=user, defaults={"secret": secret, "confirmed_at": None, "last_step": None},
        )
    account = user.email or str(user.pk)
    return {
        "secret": secret,
        "otpauth_uri": totp.provisioning_uri(secret, account=account, issuer=_issuer(user)),
        "issuer": _issuer(user),
        "account": account,
    }


@dataclass
class Passed:
    """A challenge answered correctly: who, and what kind of session it was for."""

    user: object
    device: bool
    email_entered: str
    method: str
    recovery_codes: list[str] | None = None


def confirm_set_up(token: str, code: str, *, request=None) -> Passed:
    """Accept the first code from a new authenticator, and hand out backup codes."""
    failed = None
    with transaction.atomic():
        challenge = _live_challenge(token, TwoStepChallenge.Purpose.SET_UP)
        authenticator = TwoStepAuthenticator.objects.select_for_update().filter(
            user=challenge.user, confirmed_at__isnull=True,
        ).first()
        if authenticator is None:
            raise TwoStepRefused(
                "TWO_STEP_SET_UP_NOT_STARTED", "Scan the QR code before entering a code.",
            )
        step = totp.matching_step(authenticator.secret, code or "")
        if step is None:
            failed = _count_failure(challenge)
        else:
            authenticator.confirmed_at = timezone.now()
            authenticator.last_step = step
            authenticator.save(update_fields=["confirmed_at", "last_step", "updated_at"])
            codes = _replace_recovery_codes(challenge.user)
            _close(challenge)
    if failed:
        _refuse_wrong_code(challenge, request, failed)
    log_auth_event(
        actor=challenge.user, subject=challenge.user, tenant=challenge.user.tenant,
        event=AuthEvent.TWO_STEP_SET_UP, request=request,
    )
    return Passed(challenge.user, challenge.device, challenge.email_entered, "code", codes)


def verify(token: str, *, code: str = "", recovery_code: str = "", request=None) -> Passed:
    """Accept a code from the app, or one backup code, for a waiting sign-in."""
    failed = None
    method = "recovery_code" if recovery_code and not code else "code"
    with transaction.atomic():
        challenge = _live_challenge(token, TwoStepChallenge.Purpose.VERIFY)
        user = challenge.user
        if method == "code":
            authenticator = TwoStepAuthenticator.objects.select_for_update().filter(
                user=user, confirmed_at__isnull=False,
            ).first()
            step = authenticator and totp.matching_step(
                authenticator.secret, code or "", after=authenticator.last_step,
            )
            if step is None or authenticator is None:
                failed = _count_failure(challenge)
            else:
                authenticator.last_step = step
                authenticator.save(update_fields=["last_step", "updated_at"])
                _close(challenge)
        else:
            backup = TwoStepRecoveryCode.objects.select_for_update().filter(
                user=user, code_hash=two_step_recovery_digest(recovery_code), used_at__isnull=True,
            ).first()
            if backup is None:
                failed = _count_failure(challenge)
            else:
                backup.used_at = timezone.now()
                backup.save(update_fields=["used_at", "updated_at"])
                _close(challenge)
    if failed:
        _refuse_wrong_code(challenge, request, failed)
    if method == "recovery_code":
        log_auth_event(
            actor=user, subject=user, tenant=user.tenant,
            event=AuthEvent.TWO_STEP_RECOVERY_CODE_USED, request=request,
            metadata={"recovery_codes_left": recovery_codes_left(user)},
        )
    return Passed(user, challenge.device, challenge.email_entered, method)


def _close(challenge: TwoStepChallenge) -> None:
    challenge.used_at = timezone.now()
    challenge.save(update_fields=["used_at", "updated_at"])


def _count_failure(challenge: TwoStepChallenge) -> str:
    """Count a wrong code on the challenge; the refusal code to answer with."""
    challenge.attempts += 1
    fields = ["attempts", "updated_at"]
    if challenge.attempts >= CHALLENGE_ATTEMPTS:
        challenge.used_at = timezone.now()
        fields.append("used_at")
    challenge.save(update_fields=fields)
    return "TWO_STEP_CHALLENGE_INVALID" if challenge.used_at else "TWO_STEP_CODE_INVALID"


def _refuse_wrong_code(challenge: TwoStepChallenge, request, code: str) -> None:
    """Record a wrong code where a wrong password goes, then refuse it."""
    from .auth import LoginService

    user = challenge.user
    LoginService._handle_failed_attempt(
        user, user.tenant, challenge.email_entered, request, failure_code="TWO_STEP_CODE_INVALID",
    )
    log_auth_event(
        actor=user, subject=user, tenant=user.tenant,
        event=AuthEvent.TWO_STEP_FAILED, request=request,
    )
    if code == "TWO_STEP_CHALLENGE_INVALID":
        raise TwoStepRefused(code, "Too many wrong codes. Enter your email and password again.")
    raise TwoStepRefused(code, "That code is not right. Check your authenticator app and try again.")


def _new_recovery_code() -> str:
    raw = secrets.token_hex(5)
    return f"{raw[:5]}-{raw[5:]}"


def _replace_recovery_codes(user) -> list[str]:
    codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    TwoStepRecoveryCode.objects.filter(user=user).delete()
    TwoStepRecoveryCode.objects.bulk_create([
        TwoStepRecoveryCode(user=user, code_hash=two_step_recovery_digest(code)) for code in codes
    ])
    return codes


def recovery_codes_left(user) -> int:
    return TwoStepRecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


def renew_recovery_codes(user, code: str, *, request=None) -> list[str]:
    """New backup codes for a signed-in person, who proves the app is theirs first."""
    with transaction.atomic():
        authenticator = TwoStepAuthenticator.objects.select_for_update().filter(
            user=user, confirmed_at__isnull=False,
        ).first()
        if authenticator is None:
            raise TwoStepRefused("TWO_STEP_NOT_SET_UP", "Two-step sign-in is not set up on this account.")
        step = totp.matching_step(authenticator.secret, code or "", after=authenticator.last_step)
        if step is None:
            raise TwoStepRefused(
                "TWO_STEP_CODE_INVALID",
                "That code is not right. Check your authenticator app and try again.",
            )
        authenticator.last_step = step
        authenticator.save(update_fields=["last_step", "updated_at"])
        codes = _replace_recovery_codes(user)
    log_auth_event(
        actor=user, subject=user, tenant=user.tenant,
        event=AuthEvent.TWO_STEP_RECOVERY_CODES_RENEWED, request=request,
    )
    return codes


def status_of(user) -> dict:
    """What a person's own account page shows."""
    return {
        "set_up": is_set_up(user),
        "required": required_for(user),
        "recovery_codes_left": recovery_codes_left(user),
    }


def reset(user, *, actor, request=None) -> bool:
    """Clear someone's authenticator and backup codes, so they set up again.

    Answers whether there was anything to clear. The caller checks that the
    actor may administer ``user`` and is not ``user``.
    """
    with transaction.atomic():
        removed, _ = TwoStepAuthenticator.objects.filter(user=user).delete()
        TwoStepRecoveryCode.objects.filter(user=user).delete()
        TwoStepChallenge.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
    log_auth_event(
        actor=actor, subject=user, tenant=user.tenant,
        event=AuthEvent.TWO_STEP_RESET, request=request,
    )
    return bool(removed)
