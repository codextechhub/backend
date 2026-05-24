"""
admin_provisioning.py

Converts ContactInfo + BranchPrimaryAdmin / SchoolPrimaryAdmin records
(created with invite_status=QUEUED) into real User accounts and dispatches
the invitation email.

Call provision_admin_user() immediately after creating either admin link
record inside the school/branch creation transaction.  Failures are isolated
via a savepoint so they never abort the parent school or branch creation.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("vs_schools.admin_provisioning")


# ── helpers ───────────────────────────────────────────────────────────────────

def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last').  Handles single-word names."""
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


# ── public API ────────────────────────────────────────────────────────────────

def provision_admin_user(
    *,
    contact,       # ContactInfo instance
    admin_link,    # BranchPrimaryAdmin or SchoolPrimaryAdmin instance
    school,        # School instance (always required)
    branch,        # Branch instance or None (required for BRANCH_ADMIN)
    user_type: str,   # 'SCHOOL_ADMIN' or 'BRANCH_ADMIN'
    role: str = "",   # branch_role / school_role label stored on the User row
    actor,         # the requesting User (invited_by); may be None for system
):
    """
    Create a User + UserInvitation and send the invite email for a queued admin.

    Wrapped in its own savepoint so a failure here (e.g. duplicate email from a
    concurrent request) is isolated and never rolls back the parent transaction.
    """
    from vs_user.models import User
    from vs_user.services.invitation import InvitationService
    from vs_user.tasks import send_invitation_email_task
    from vs_schools.models import InviteStatus

    email = contact.email.lower().strip()

    try:
        from vs_rbac.models import SchoolRoleTemplate, SchoolUserRoleAssignment
        with transaction.atomic():  # savepoint — rollback here if anything fails
            # Idempotent: if the user already exists just stamp the link as sent.
            existing = User.objects.filter(email=email).first()
            if existing:
                logger.warning(
                    "provision_admin_user: %s already has a User account; "
                    "skipping creation and marking link as SENT",
                    email,
                )
                admin_link.invite_status = InviteStatus.SENT
                admin_link.invite_sent_at = timezone.now()
                admin_link.save(update_fields=["invite_status", "invite_sent_at"])
                return existing

            first_name, last_name = _split_name(contact.full_name)
            invited_by = actor if isinstance(actor, User) else None
            role_obj = SchoolRoleTemplate.objects.filter(id=role, school=school).first() if role else None

            # A school admin or branch admin without a role is a half-broken
            # account: they receive the invitation email, activate it, and
            # then can do nothing. Fail loud here instead of silently creating
            # the user and dispatching the email — the outer savepoint will
            # roll back, and the admin link stays in QUEUED so the operator
            # can investigate (typically: the prebuilt role template wasn't
            # seeded, or the school's per-school SchoolRoleTemplate is missing).
            if not role_obj:
                raise ValueError(
                    f"Refusing to provision {email} ({user_type}) without a role assignment. "
                    f"Expected SchoolRoleTemplate id={role!r} on school {school.id}."
                )

            user = User.objects.create_user(
                email=email,
                password=None,
                first_name=first_name,
                last_name=last_name,
                gender="",
                phone=getattr(contact, "phone", "") or "",
                user_type=user_type,
                role=role_obj.name,
                school=school,
                branch=branch,
                invited_by=invited_by,
                status=User.Status.PENDING,
                is_active=False,
                is_staff=False,
            )

            SchoolUserRoleAssignment.objects.create(
                user=user,
                role=role_obj,
                school=user.school,
                assigned_by=invited_by,
            )

            # Invitation record — expiry gate for the activation link.
            InvitationService.create(user=user, invited_by=invited_by or user)

            send_invitation_email_task.delay(str(user.activation_key))

            # Mark the admin link record so it is not re-processed.
            admin_link.invite_status = InviteStatus.SENT
            admin_link.invite_sent_at = timezone.now()
            admin_link.save(update_fields=["invite_status", "invite_sent_at"])

            logger.info(
                "provision_admin_user: created User %s (type=%s) and dispatched invite",
                email,
                user_type,
            )
            return user

    except Exception as exc:  # noqa: BLE001
        # Log but do not re-raise — admin provisioning failure must never
        # abort the school/branch creation that triggered it.
        logger.error(
            "provision_admin_user: failed for %s — %s",
            email,
            exc,
            exc_info=True,
        )
        return None
