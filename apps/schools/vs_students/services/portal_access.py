"""A pupil's own sign-in account for the pupil portal.

A pupil's account is an ordinary school account linked to their record
(``Student.user``). It holds no role and no permission key: what it reaches is
computed from that link on every request, so taking the account away, or the
pupil leaving the roll, ends their reach on the next request with nothing to
expire.

The pupil activates it the way everybody else activates an account, through
the invitation link. The link goes to the pupil's own email where the record
has one and to their primary guardian's (``sign_in.guardian_delivery_addresses``),
because many pupils have no address of their own and a parent is who helps
them through it either way. A pupil with an email signs in with it or with
their admission number; a pupil without one has an account with no address
(``User.objects.create_user_without_email``) and signs in with the admission
number alone.

Every issue, resend and revoke writes an audit event naming the pupil, so a
family asking who could sign in as their child, and since when, gets an answer.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import PORTAL_STATUSES
from ..exceptions import (
    PortalAccessAlreadyGiven,
    PortalAccessNotGiven,
    PortalEmailInUse,
    PortalInviteNotPending,
    PortalNoDeliveryAddress,
    PortalNoWayToSignIn,
    PortalPupilNotOnRoll,
)

#: What a staff screen calls the state of a pupil's access.
NONE, INVITED, ACTIVE, REVOKED = "none", "invited", "active", "revoked"


@dataclass(frozen=True)
class BulkOutcome:
    """One pupil's result in a bulk issue: ``issued``, ``already`` or ``refused``."""

    student_id: int
    outcome: str
    message: str = ""


def state_of(student) -> str:
    """Where this pupil's access stands, in one word."""
    from vs_user.models import User

    user = student.user
    if user is None:
        return NONE
    if user.status == User.Status.ACTIVE:
        return ACTIVE
    if user.status == User.Status.PENDING:
        return INVITED
    return REVOKED


def describe(student) -> dict:
    """The access block a staff screen shows on the pupil's record."""
    from vs_user.models import UserInvitation

    user = student.user
    state = state_of(student)
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
            "admission_number": student.student_number or "",
            "email": (user.email or "") if user is not None else "",
        },
    }


def _guardian_email(student) -> str:
    from .guardians import primary_for

    guardian = primary_for(student)
    return (guardian.email or "").strip() if guardian is not None else ""


def _audit(student, action, actor, summary, **metadata):
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=action,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=summary,
        metadata=metadata,
    )


@transaction.atomic
def issue(student, *, actor):
    """Give *student* their own account and send the invitation.

    Refuses, writing nothing, when the pupil is not on the roll, already holds
    working access, could not sign in (no admission number and no email),
    has nobody to send the link to, or has an email that another account at
    this school already uses. A pupil whose earlier access was revoked gets
    the same account back, reopened and invited afresh, so their sign-in
    history stays one history.
    """
    from vs_user.email_normalization import normalize_optional_email
    from vs_user.models import User
    from vs_user.services.invitation import InvitationService

    student = type(student).objects.select_for_update().get(pk=student.pk)
    if student.status not in PORTAL_STATUSES:
        raise PortalPupilNotOnRoll(
            f"{student.first_name} is not on the roll, so they cannot be given "
            f"portal access.",
        )
    state = state_of(student)
    if state in (INVITED, ACTIVE):
        raise PortalAccessAlreadyGiven(
            f"{student.first_name} already has portal access. Resend the "
            f"invitation instead if it has not reached them.",
        )

    email = normalize_optional_email(student.email)
    if not email and not (student.student_number or "").strip():
        raise PortalNoWayToSignIn()
    if not email and not _guardian_email(student):
        raise PortalNoDeliveryAddress(
            f"There is nowhere to send {student.first_name}'s invitation. Add "
            f"an email to their record or to their primary guardian first.",
        )

    user = student.user
    if user is None:
        if email and User.objects.filter(tenant=student.tenant, email=email).exists():
            raise PortalEmailInUse(
                f"{student.first_name}'s email is already used by another "
                f"account at this school. Correct the email on their record, "
                f"or clear it so they sign in with their admission number.",
            )
        fields = dict(
            tenant=student.tenant, branch=student.branch,
            first_name=student.first_name, last_name=student.last_name,
            status=User.Status.PENDING, is_active=False,
        )
        user = (
            User.objects.create_user(email, **fields) if email
            else User.objects.create_user_without_email(**fields)
        )
        student.user = user
        student.save(update_fields=["user", "updated_at"])
    else:
        user.status = User.Status.PENDING
        user.is_active = False
        user.branch = student.branch
        user.save(update_fields=["status", "is_active", "branch", "updated_at"])

    invitation, token = InvitationService.create(user=user, invited_by=actor)
    _queue_invitation(invitation, token, user, actor)

    _audit(
        student, AuditActionType.STUDENT_PORTAL_ACCESS_ISSUED, actor,
        f"{student.full_name} was given portal access.",
        signs_in_with_email=bool(email),
    )
    return student


@transaction.atomic
def reissue(student, *, actor):
    """Send the invitation again, with a fresh link, to a pupil not yet signed in."""
    from vs_user.services.invitation import InvitationService

    if state_of(student) != INVITED:
        raise PortalInviteNotPending(
            f"{student.first_name} has no invitation waiting. Give them "
            f"portal access first, or they have already activated it.",
        )
    InvitationService.resend(student.user, requested_by=actor)
    _audit(
        student, AuditActionType.STUDENT_PORTAL_INVITE_RESENT, actor,
        f"{student.full_name}'s portal invitation was sent again.",
    )
    return student


@transaction.atomic
def revoke(student, *, actor, reason="", system=False):
    """Close the pupil's account and end every session it holds.

    The link to the record is kept, so a pupil who returns is given the same
    account back and their sign-in history stays whole. ``system`` marks a
    revocation the roll made on its own (the pupil left), which names no
    actor.
    """
    if state_of(student) not in (INVITED, ACTIVE):
        if system:
            return student
        raise PortalAccessNotGiven(
            f"{student.first_name} has no portal access to take away.",
        )
    _close_account(student.user)
    _audit(
        student, AuditActionType.STUDENT_PORTAL_ACCESS_REVOKED,
        None if system else actor,
        f"{student.full_name}'s portal access was taken away."
        + (f" {reason}" if reason else ""),
        automatic=system,
    )
    return student


def issue_for_students(students, *, actor) -> list[BulkOutcome]:
    """Issue to each pupil in turn; one refusal never stops the others."""
    from ..exceptions import PortalAccessError

    out = []
    for student in students:
        if state_of(student) in (INVITED, ACTIVE):
            out.append(BulkOutcome(student.pk, "already"))
            continue
        try:
            issue(student, actor=actor)
        except PortalAccessError as exc:
            out.append(BulkOutcome(student.pk, "refused", exc.message))
        else:
            out.append(BulkOutcome(student.pk, "issued"))
    return out


def _close_account(user):
    """Deactivate *user* and end its sessions, as the account engine does for staff."""
    from vs_user.models import User
    from vs_user.services.audit import blacklist_all_user_tokens

    user.status = User.Status.DEACTIVATED
    user.is_active = False
    user.save(update_fields=["status", "is_active", "updated_at"])
    blacklist_all_user_tokens(user, reason="DEACTIVATED")


def _queue_invitation(invitation, token, user, actor):
    from vs_user.tasks import queue_invitation_email

    queue_invitation_email(
        invitation_id=invitation.pk,
        token=token,
        user=user,
        owner_id=str(actor.id) if actor else None,
        label=f"Portal invitation for {user.full_name}",
    )
