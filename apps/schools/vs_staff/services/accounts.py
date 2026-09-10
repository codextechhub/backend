"""Account actions a school may take on its own people.

Three keys have been seeded, marked sensitive, granted to school administrators
and bundled into a permission group since the scope audit, and until now no view
in the repository declared any of them. So a school could invite somebody and
could never suspend them, while the box in its role builder said it could. These
are the endpoints behind those keys.

**Nothing here writes ``User.status``.** Each action calls the same service the
platform's own endpoint calls, so the eligibility rules, the auth event, the
session blacklisting and the lockout clearing are asked once and answered once.

Reactivate and unlock are separate actions on one key because they answer
different questions. Reactivate ends an administrative suspension; unlock clears
a security lockout after failed sign-ins. A school that confuses the two is a
school that thinks its teacher was suspended for mistyping a password.

FRD M12 v2.1, FR-009.
"""
from __future__ import annotations

from ..exceptions import AccountNotEligible, CannotActOnSelf
from .scoping import is_self


def _call(method, user, actor, request):
    """Run an identity service and translate its refusal into ours.

    The service raises ``ValueError`` carrying a dict; the handler here reads
    exactly three attributes, so the message is lifted out rather than the
    exception being allowed through as a 500.
    """
    try:
        return method(user, actor, request=request)
    except ValueError as error:
        payload = error.args[0] if error.args else {}
        message = payload.get("message") if isinstance(payload, dict) else str(payload)
        raise AccountNotEligible(message or AccountNotEligible.default_message)


def suspend(staff, *, actor, request=None):
    """Close a login without touching employment.

    Suspending an account does not make somebody unemployed, and this is one
    half of the pair the acceptance criteria assert in both directions: the
    other is that moving employment to SUSPENDED does suspend the account.
    """
    # Suspending yourself signs you out and leaves nobody holding the school.
    if is_self(actor, staff):
        raise CannotActOnSelf(
            "You cannot suspend your own account. Ask another administrator to "
            "do it.",
        )

    from vs_user.services.user import UserStatusService

    return _call(UserStatusService.suspend, staff.user, actor, request)


def reactivate(staff, *, actor, request=None):
    from vs_user.services.user import UserStatusService

    return _call(UserStatusService.reactivate, staff.user, actor, request)


def unlock(staff, *, actor, request=None):
    """Clear a security lockout. Not a suspension, and never rendered as one.

    Mrs. Okafor mistypes her password three times on a Tuesday morning and is
    locked out. She is employed, at work, and standing in front of JSS1 B at
    nine o'clock, and her employment status does not move.
    """
    from vs_user.services.user import UserStatusService

    return _call(UserStatusService.unlock, staff.user, actor, request)


def change_email(staff, new_email, *, actor, request=None):
    """Change the address an account signs in with.

    Refused for an address already in use at this school, and it reveals nothing
    about any other school: the uniqueness is per tenant, so the same address may
    legitimately be an account somewhere else and a school must not learn so.
    """
    from vs_user.services.user import EmailChangeService

    try:
        return EmailChangeService.change_email(
            staff.user, new_email, actor, request=request,
        )
    except ValueError as error:
        payload = error.args[0] if error.args else {}
        message = payload.get("message") if isinstance(payload, dict) else str(payload)
        raise AccountNotEligible(message or "That email address cannot be used.")
