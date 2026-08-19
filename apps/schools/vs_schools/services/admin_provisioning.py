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
    branch,        # Branch instance, or None for a school-wide posting
    role: str = "",   # TenantRoleTemplate key for the tenant role assignment
    actor,         # the requesting User (invited_by); may be None for system
):
    """
    Create a User + UserInvitation and send the invite email for a queued admin.

    Wrapped in its own savepoint so a failure here (e.g. duplicate email from a
    concurrent request) is isolated and never rolls back the parent transaction.

    The account is created as ordinary ``STAFF``. It used to take a
    ``user_type`` of 'SCHOOL_ADMIN' or 'BRANCH_ADMIN' from the caller, and the
    two said nothing the rest of the arguments did not already say: the reach is
    ``branch`` (a branch, or None for the whole school) and the authority is
    ``role``. A persona that mirrors two other arguments is a third copy of the
    truth waiting to disagree with them, so it is gone.
    """
    from vs_user.email_normalization import normalize_email
    from vs_user.models import User
    from vs_user.services.invitation import InvitationService
    from vs_user.tasks import send_invitation_email_task
    from ..models import InviteStatus

    email = normalize_email(contact.email)
    tenant = school.tenant

    try:
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        with transaction.atomic():  # savepoint - rollback here if anything fails
            # Idempotent: if this school already has an account on that address
            # just stamp the link as sent.
            #
            # Scoped to THIS school's tenant, and the scope is the whole point.
            # Unscoped, "already exists" meant "exists anywhere on the
            # platform", and the row it returned was handed back to the caller
            # as the new school's administrator: CodeX creates Greenfield with
            # ada.okoye@example.test as primary admin, Ada already administers
            # Bright Star, and Greenfield's admin link is stamped SENT pointing
            # at Bright Star's account. No exception, no error log, and the
            # invitation email is never sent because the account it "found"
            # was activated months ago.
            #
            # Nothing but the school-create serializer's own pre-check kept
            # that unreachable, and this service has three callers.
            #
            # Exact match on a normalised address is exhaustive because every
            # stored address is lowercase (User.save plus
            # ck_user_email_lowercase).
            existing = User.objects.filter(email=email, tenant=tenant).first()
            if existing:
                logger.warning(
                    "provision_admin_user: %s already has a User account at "
                    "tenant %s; skipping creation and marking link as SENT",
                    email, getattr(tenant, "slug", tenant),
                )
                admin_link.invite_status = InviteStatus.SENT
                admin_link.invite_sent_at = timezone.now()
                admin_link.save(update_fields=["invite_status", "invite_sent_at"])
                return existing

            first_name, last_name = _split_name(contact.full_name)
            invited_by = actor if isinstance(actor, User) else None
            role_obj = (
                TenantRoleTemplate.objects.filter(tenant=tenant, key=role).first()
                if role else None
            )

            # An administrator without a role is a half-broken
            # account: they receive the invitation email, activate it, and
            # then can do nothing. Fail loud here instead of silently creating
            # the user and dispatching the email - the outer savepoint will
            # roll back, and the admin link stays in QUEUED so the operator
            # can investigate (typically: the prebuilt role template wasn't
            # seeded, or the tenant's TenantRoleTemplate is missing).
            if not role_obj:
                raise ValueError(
                    f"Refusing to provision {email} without a role assignment. "
                    f"Expected TenantRoleTemplate key={role!r} on tenant {getattr(tenant, 'slug', tenant)}."
                )

            user = User.objects.create_user(
                email=email,
                password=None,
                first_name=first_name,
                last_name=last_name,
                gender="",
                phone=getattr(contact, "phone", "") or "",
                user_type=User.UserType.STAFF,
                role=role_obj.name,
                tenant=tenant,
                branch=branch,
                invited_by=invited_by,
                status=User.Status.PENDING,
                is_active=False,
                is_staff=False,
            )

            TenantUserRoleAssignment.objects.create(
                tenant=tenant,
                user=user,
                role=role_obj,
                branch=role_obj.branch,
                assigned_by=invited_by,
            )

            # Invitation record - expiry gate for the activation link.
            InvitationService.create(user=user, invited_by=invited_by or user)

            send_invitation_email_task.delay(
                str(user.activation_key),
                # Owner is whoever provisioned the school admin; a provisioning
                # run with no actor stays a system row (owner=None).
                _job_owner_id=str(invited_by.id) if invited_by else None,
                _job_label=f"Invitation email to {user.email}",
                _job_kind="email",
                # Fan-out plumbing: one bell notification per invited row is spam.
                _job_notify=False,
            )

            # Mark the admin link record so it is not re-processed.
            admin_link.invite_status = InviteStatus.SENT
            admin_link.invite_sent_at = timezone.now()
            admin_link.save(update_fields=["invite_status", "invite_sent_at"])

            logger.info(
                "provision_admin_user: created User %s (role=%s, branch=%s) "
                "and dispatched invite",
                email,
                role_obj.key,
                getattr(branch, "pk", None),
            )
            return user

    except Exception as exc:  # noqa: BLE001
        # Log but do not re-raise - admin provisioning failure must never
        # abort the school/branch creation that triggered it.
        logger.error(
            "provision_admin_user: failed for %s - %s",
            email,
            exc,
            exc_info=True,
        )
        return None
