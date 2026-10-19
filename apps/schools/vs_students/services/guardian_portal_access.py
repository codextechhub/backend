"""A guardian's own sign-in account for the parent portal.

A guardian is a person, and their account, where they have one, is an ordinary
school account (``vs_user.User``) linked to their record (``Guardian.user``).
It holds no role and no permission key: what it reaches is computed from that
link on every request, so taking the link away ends a parent's reach on the
next request with nothing to expire.

One person is one account within a school. A teacher whose own child attends
is already a user of the school, so her guardian record is linked to the
account she has and a second one is never made. A parent with children at
several branches has one account, because the account carries no branch: a
guardian is school-level, and what a parent sees of each child is decided by
the child's own record.

The account is activated the way everybody else activates one, through the
invitation link, which goes to the guardian's own email and to no one else.
Every issue, resend and revoke writes an audit event naming the guardian.

Taking access away depends on what else the account is used for. An account
that exists only for the parent portal is deactivated and kept linked, so a
parent who returns gets the same account back and their sign-in history stays
one history. An account that is also used for anything else (a staff role, a
staff record, a pupil's own sign-in) is never deactivated from here: the link
to the guardian is removed instead, which ends portal reach and leaves the
other sign-in untouched.
"""
from __future__ import annotations

from django.db import transaction

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import PORTAL_STATUSES
from ..exceptions import (
    PortalAccessAlreadyGiven,
    PortalAccessNotGiven,
    PortalAccountNotUsable,
    PortalEmailInUse,
    PortalGuardianNoChildOnRoll,
    PortalGuardianNoEmail,
    PortalInviteNotPending,
)
from ..models import Guardian, Student, StudentGuardian
from .portal_access import (
    ACTIVE,
    INVITED,
    NONE,
    REVOKED,
    _close_account,
    _queue_invitation,
)

__all__ = [
    "ACTIVE", "INVITED", "NONE", "REVOKED",
    "describe", "issue", "reissue", "revoke", "state_of",
]


def state_of(guardian) -> str:
    """Where this guardian's access stands, in one word."""
    from vs_user.models import User

    if guardian.user_id is None:
        return NONE
    user = guardian.user
    if user.status == User.Status.ACTIVE:
        return ACTIVE
    if user.status == User.Status.PENDING:
        return INVITED
    return REVOKED


def describe(guardian) -> dict:
    """The access block a staff screen shows on the guardian's record."""
    from vs_user.models import UserInvitation

    user = guardian.user if guardian.user_id is not None else None
    state = state_of(guardian)
    invitation = (
        UserInvitation.objects.filter(user=user).first() if user is not None else None
    )
    return {
        "has_access": state in (INVITED, ACTIVE),
        "status": state,
        "invited_at": invitation.created_at if invitation else None,
        "invitation_expires_at": (
            invitation.expires_at if invitation and state == INVITED else None
        ),
        "last_sign_in_at": user.last_login_at if user is not None else None,
        "sign_in_identifiers": {
            "email": ((user.email if user is not None else guardian.email) or ""),
        },
    }


def is_parent_only(user) -> bool:
    """Whether *user* exists for the parent portal and for nothing else.

    A staff flag, a role at any school, a staff record, or being a pupil's own
    sign-in each make the account somebody's working or learning login, which
    a parent-portal action must never close.
    """
    if user.is_staff or user.is_superuser or user.is_platform_user:
        return False
    if user.tenant_role_assignments.exists():
        return False
    if Student.all_objects.filter(user=user).exists():
        return False
    return not hasattr(user, "staff_profile")


def _has_child_on_roll(guardian) -> bool:
    return StudentGuardian.objects.filter(
        tenant_id=guardian.tenant_id, guardian=guardian,
        student__status__in=PORTAL_STATUSES,
    ).exists()


def _audit(guardian, action, actor, summary, **metadata):
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=action,
        entity_type="Guardian", entity_id=str(guardian.pk),
        entity_label=guardian.full_name,
        tenant=guardian.tenant, actor_user=actor,
        summary=summary,
        metadata=metadata,
    )


def _account_names(guardian) -> tuple[str, str]:
    """First and last name for the account, from the parts or the one-line name."""
    first = (guardian.first_name or "").strip()
    last = (guardian.last_name or "").strip()
    if not (first and last):
        words = (guardian.full_name or "").split()
        first = first or (words[0] if words else "Guardian")
        last = last or (" ".join(words[1:]) or first)
    return first, last


def _invite(user, actor):
    """Queue a fresh invitation to *user*'s own address."""
    from vs_user.services.invitation import InvitationService

    invitation, token = InvitationService.create(user=user, invited_by=actor)
    _queue_invitation(invitation, token, user, actor)


def _reopen(user):
    """Put a closed parent-only account back to awaiting activation."""
    from vs_user.models import User

    user.status = User.Status.PENDING
    user.is_active = False
    user.save(update_fields=["status", "is_active", "updated_at"])


@transaction.atomic
def issue(guardian, *, actor):
    """Give *guardian* the parent portal and send the invitation.

    Refuses, writing nothing, when the invitation is already waiting, when the
    guardian has no email, when none of their children is on the roll, when
    another guardian's account already uses their email, or when the account
    at their email is one a parent invitation must not reopen.

    A guardian who already has a working account is answered as active and
    nothing is created or sent. Otherwise the account is, in order: the one
    already linked, the one the school already holds at the guardian's email
    (a teacher whose own child attends), or a new one. It is made for the
    guardian's school with no branch, no role and no permission, inactive
    until the invitation is followed. A revoked parent-only account is
    reopened and invited afresh.
    """
    from vs_user.email_normalization import normalize_optional_email
    from vs_user.models import User, UserInvitation

    guardian = Guardian.objects.select_for_update().get(pk=guardian.pk)
    state = state_of(guardian)
    if state == INVITED:
        raise PortalAccessAlreadyGiven(
            f"{guardian.first_name or guardian.full_name} already has an invitation "
            f"waiting. Resend it instead if it has not reached them.",
        )
    if state == ACTIVE:
        return guardian

    email = normalize_optional_email(guardian.email)
    if not email:
        raise PortalGuardianNoEmail(
            f"There is nowhere to send {guardian.full_name}'s invitation. Add an "
            f"email to their record first.",
        )
    if not _has_child_on_roll(guardian):
        raise PortalGuardianNoChildOnRoll(
            f"{guardian.full_name} has no child on the roll, so there is nothing "
            f"for the parent portal to show. Link a child who is on the roll first.",
        )

    user = guardian.user
    linked_existing = False
    if user is None:
        user = User.objects.filter(tenant=guardian.tenant, email=email).first()
        if user is not None:
            if Guardian.objects.filter(
                tenant=guardian.tenant, user=user,
            ).exclude(pk=guardian.pk).exists():
                raise PortalEmailInUse(
                    f"{guardian.full_name}'s email is already used by another "
                    f"account at this school. Correct the email on their record.",
                )
            if Student.all_objects.filter(user=user).exists():
                raise PortalAccountNotUsable(
                    f"{guardian.full_name}'s email is a pupil's own sign-in at this "
                    f"school. Correct the email on their record.",
                )
            guardian.user = user
            guardian.save(update_fields=["user", "updated_at"])
            linked_existing = True

    if user is None:
        first, last = _account_names(guardian)
        user = User.objects.create_user(
            email, tenant=guardian.tenant, branch=None,
            first_name=first, last_name=last,
            status=User.Status.PENDING, is_active=False,
        )
        guardian.user = user
        guardian.save(update_fields=["user", "updated_at"])
        _invite(user, actor)
    elif user.status == User.Status.ACTIVE:
        pass
    elif user.status == User.Status.PENDING:
        if not UserInvitation.objects.filter(user=user, is_used=False).exists():
            _invite(user, actor)
    elif user.status == User.Status.DEACTIVATED and is_parent_only(user):
        _reopen(user)
        _invite(user, actor)
    else:
        raise PortalAccountNotUsable(
            f"The account at {guardian.full_name}'s email cannot be used for the "
            f"parent portal. Correct the email on their record, or ask the person "
            f"who manages that account.",
        )

    _audit(
        guardian, AuditActionType.STUDENT_PORTAL_ACCESS_ISSUED, actor,
        f"{guardian.full_name} was given parent portal access.",
        linked_existing_account=linked_existing,
    )
    return guardian


@transaction.atomic
def reissue(guardian, *, actor):
    """Send the invitation again, with a fresh link, to a guardian not yet signed in."""
    from vs_user.services.invitation import InvitationService

    guardian = Guardian.objects.select_for_update().get(pk=guardian.pk)
    if state_of(guardian) != INVITED:
        raise PortalInviteNotPending(
            f"{guardian.full_name} has no invitation waiting. Give them parent "
            f"portal access first, or they have already activated it.",
        )
    InvitationService.resend(guardian.user, requested_by=actor)
    _audit(
        guardian, AuditActionType.STUDENT_PORTAL_INVITE_RESENT, actor,
        f"{guardian.full_name}'s parent portal invitation was sent again.",
    )
    return guardian


@transaction.atomic
def revoke(guardian, *, actor, reason=""):
    """End the guardian's reach into the parent portal.

    Returns ``(guardian, account_closed)``. A parent-only account is
    deactivated, its sessions ended, and the link kept so state reads
    ``revoked`` and a returning parent gets the same account back. An account
    used for anything else is left exactly as it is and only the link to the
    guardian is removed, so state reads ``none`` and the other sign-in keeps
    working.
    """
    guardian = Guardian.objects.select_for_update().get(pk=guardian.pk)
    if state_of(guardian) not in (INVITED, ACTIVE):
        raise PortalAccessNotGiven(
            f"{guardian.full_name} has no parent portal access to take away.",
        )
    user = guardian.user
    account_closed = is_parent_only(user)
    if account_closed:
        _close_account(user)
    else:
        guardian.user = None
        guardian.save(update_fields=["user", "updated_at"])
    _audit(
        guardian, AuditActionType.STUDENT_PORTAL_ACCESS_REVOKED, actor,
        f"{guardian.full_name}'s parent portal access was taken away."
        + (f" {reason}" if reason else ""),
        account_closed=account_closed,
    )
    return guardian, account_closed
